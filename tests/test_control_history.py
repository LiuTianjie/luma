from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.control import database, history
from luma.errors import LumaError


class ControlHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.state = {"clusterId": "test", "deployToken": "secret", "buildRuns": {}, "deploymentEvents": []}

    def build(self, identifier, created=100, *, app="api", status="succeeded", events=0):
        value = {"id": identifier, "status": status, "createdAt": created, "updatedAt": created,
                 "source": "https://git.example.com/org/repo", "request": {"repository": "org/repo", "ref": "main"},
                 "result": {"service": app}, "events": [{"name": "build", "message": f"line {index}"} for index in range(events)]}
        self.state["buildRuns"][identifier] = value
        return value

    def deployment(self, identifier, created=100, *, app="api", source="cli", status="active", steps=0):
        value = {"id": identifier, "name": app, "slug": app, "kind": "service", "origin": source,
                 "status": status, "createdAt": created, "stepCount": steps,
                 "steps": [{"name": "deploy", "message": f"step {index}"} for index in range(steps)]}
        self.state["deploymentEvents"].append(value)
        return value

    def migrate(self):
        (Path(self.temp.name) / "control.json").write_text(json.dumps(self.state))
        database.ensure_initialized()

    def write(self):
        with database.transaction() as conn:
            database.write_state(conn, self.state)

    def test_unified_cursor_orders_timestamp_id_kind_ties_and_freezes_insert_watermark(self):
        self.build("same", 100)
        self.deployment("same", 100)
        self.build("z", 100)
        self.deployment("older", 90)
        self.migrate()
        first = history.list_history({"limit": "2"})
        self.assertEqual([(row["id"], row["kind"]) for row in first["items"]], [("z", "build"), ("same", "deployment")])
        self.assertTrue(first["page"]["hasMore"])
        # New rows, including backdated rows, must not enter an existing listing.
        self.build("new", 200)
        self.build("backdated", 1)
        self.write()
        second = history.list_history({"limit": "2", "cursor": first["page"]["nextCursor"]})
        self.assertEqual([(row["id"], row["kind"]) for row in second["items"]], [("same", "build"), ("older", "deployment")])
        self.assertFalse(second["page"]["hasMore"])
        self.assertIsNone(second["page"]["nextCursor"])
        self.assertEqual(len(history.list_history()["items"]), 6)

    def test_legacy_deployment_ties_preserve_newest_insert_and_separate_cursor_order(self):
        self.deployment("zzz", 100)
        self.deployment("aaa", 100)
        self.migrate()
        first = history.list_deployments({"limit": 1})
        self.assertEqual(first["events"][0]["id"], "aaa")
        second = history.list_deployments({"limit": 1, "cursor": first["page"]["nextCursor"]})
        self.assertEqual(second["events"][0]["id"], "zzz")
        self.assertFalse(second["page"]["hasMore"])
        # Unified timeline retains its documented timestamp/id/kind ordering.
        unified = history.list_history({"kind": "deployment", "limit": 1})
        self.assertEqual(unified["items"][0]["id"], "zzz")
        with self.assertRaisesRegex(LumaError, "filters"):
            history.list_deployments({"cursor": unified["page"]["nextCursor"]})

    def test_retry_relationships_are_visible_without_rewriting_prior_failure(self):
        self.build("parent", 100, status="failed", events=2)
        child = self.build("child", 101)
        child.update(retryOf="parent", retryRootId="parent")
        self.migrate()
        row = history.list_history()["items"][0]
        self.assertEqual((row["retryOf"], row["retryRootId"]), ("parent", "parent"))
        self.assertEqual(history.get_build("child")["run"]["retryOf"], "parent")
        self.assertEqual(history.get_build("parent")["run"]["status"], "failed")
        self.assertEqual(len(history.get_build("parent")["run"]["events"]), 2)

    def test_filters_share_sql_scope_and_cursor_cannot_change_filters(self):
        self.deployment("match2", 200, source="dashboard", status="failed")
        self.deployment("match1", 100, source="dashboard", status="failed")
        self.deployment("wrong-app", 150, app="worker", source="dashboard", status="failed")
        self.deployment("wrong-source", 150, source="cli", status="failed")
        self.build("wrong-kind", 150, status="failed")
        self.migrate()
        query = {"limit": "1", "app": "api", "status": "failed", "source": "dashboard", "kind": "deployment", "since": "1970-01-01T00:01:40Z", "until": "200"}
        first = history.list_history(query)
        self.assertEqual(first["items"][0]["id"], "match2")
        cursor = first["page"]["nextCursor"]
        second = history.list_history({**query, "cursor": cursor})
        self.assertEqual(second["items"][0]["id"], "match1")
        with self.assertRaisesRegex(LumaError, "filters"):
            history.list_history({**query, "cursor": cursor, "app": "worker"})
        with self.assertRaisesRegex(LumaError, "filters"):
            history.list_builds({"cursor": cursor})

    def test_step_pages_are_oldest_first_and_resume_without_replay_or_new_appends(self):
        run = self.build("run", events=5)
        self.migrate()
        first = history.get_history("build", "run", {"limit": "2"})
        self.assertEqual([step["message"] for step in first["events"]], ["line 0", "line 1"])
        run["events"].append({"message": "new event"})
        self.write()
        second = history.get_history("build", "run", {"limit": 2, "cursor": first["page"]["nextCursor"]})
        third = history.get_history("build", "run", {"limit": 2, "cursor": second["page"]["nextCursor"]})
        self.assertEqual([step["message"] for step in second["events"] + third["events"]], ["line 2", "line 3", "line 4"])
        self.assertFalse(third["page"]["hasMore"])
        self.assertEqual(history.get_history("build", "run")["item"]["stepCount"], 6)
        with self.assertRaisesRegex(LumaError, "record"):
            history.get_history("deployment", "run", {"cursor": first["page"]["nextCursor"]})

    def test_legacy_records_migrate_once_without_100_200_300_deletions(self):
        for index in range(130):
            self.build(f"build-{index:03}", index, status="running" if index == 0 else "succeeded", events=501 if index == 0 else 0)
        for index in range(230):
            self.deployment(f"deploy-{index:03}", index)
        self.migrate()
        with patch("luma.control.database.read_state", side_effect=AssertionError("history must not materialize control state")):
            items, cursor = [], ""
            while True:
                page = history.list_history({"limit": "100", "cursor": cursor})
                items.extend(page["items"])
                cursor = page["page"]["nextCursor"]
                if not cursor:
                    break
            self.assertEqual(len(items), 360)
            detail = history.get_build("build-000", {"limit": 100})
            self.assertEqual(detail["run"]["status"], "running")
            self.assertEqual(len(detail["run"]["events"]), 100)
            self.assertTrue(detail["eventsPage"]["hasMore"])
            self.assertEqual(history.get_history("build", "build-000")["item"]["stepCount"], 501)
        self.assertTrue((Path(self.temp.name) / "control.json.pre-sqlite.bak").exists())
        # Legacy file changes after cutover cannot replace SQLite history.
        (Path(self.temp.name) / "control.json").write_text('{}')
        self.assertEqual(history.get_build("build-000")["run"]["status"], "running")

    def test_expired_step_metadata_is_visible_in_unified_and_legacy_details(self):
        build = self.build("expired-build")
        deploy = self.deployment("expired-deploy")
        for record in (build, deploy):
            record.update(detailsExpiredAt=123456, detailsRetentionDays=30)
        self.migrate()
        for kind, identifier in (("build", "expired-build"), ("deployment", "expired-deploy")):
            detail = history.get_history(kind, identifier)
            self.assertEqual(detail["events"], [])
            self.assertEqual(detail["item"]["detailsExpiredAt"], 123456)
            self.assertEqual(detail["record"]["detailsRetentionDays"], 30)
        self.assertEqual(history.get_build("expired-build")["run"]["detailsExpiredAt"], 123456)
        self.assertEqual(history.get_deployment("expired-deploy")["event"]["detailsRetentionDays"], 30)

    def test_compatibility_shapes_omit_internal_fields_and_page_details(self):
        self.build("build", events=2)
        self.deployment("deploy", steps=2)
        self.migrate()
        builds = history.list_builds()
        self.assertEqual(builds["runs"][0]["repository"], "org/repo")
        self.assertNotIn("events", builds["runs"][0])
        deployments = history.list_deployments()
        self.assertNotIn("steps", deployments["events"][0])
        self.assertNotIn("__luma_event_streams__", json.dumps(deployments))
        detail = history.get_deployment("deploy", {"limit": 1})
        self.assertEqual(len(detail["event"]["steps"]), 1)
        self.assertTrue(detail["stepsPage"]["hasMore"])
        self.assertEqual(history.get_build("build")["run"]["request"]["ref"], "main")
        self.assertNotIn("secret", json.dumps(history.list_history()))

    def test_limits_empty_records_and_invalid_inputs(self):
        self.build("empty")
        self.migrate()
        self.assertEqual(history.list_history()["page"]["limit"], 50)
        self.assertEqual(history.get_history("build", "empty")["events"], [])
        self.assertIsNone(history.get_history("build", "empty")["page"]["nextCursor"])
        for query in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": "2.5"}, {"cursor": "!bad"}, {"kind": "unknown"}, {"source": "unknown"}, {"since": "2026-01-01"}, {"since": 20, "until": 10}):
            with self.subTest(query=query), self.assertRaises(LumaError):
                history.list_history(query)
        with self.assertRaisesRegex(LumaError, "not found"):
            history.get_history("build", "missing")

    def test_malformed_cursor_key_is_rejected_without_typeerror(self):
        self.build("a")
        self.build("b")
        self.migrate()
        cursor = history.list_history({"limit": 1})["page"]["nextCursor"]
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        value["key"][2] = {}
        cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
        with self.assertRaises(LumaError):
            history.list_history({"cursor": cursor})

    def test_query_values_are_bound_parameters(self):
        self.build("a", app="api")
        self.migrate()
        self.assertEqual(history.list_history({"app": "api' OR 1=1 --"})["items"], [])
        self.assertEqual(len(history.list_history()["items"]), 1)


class HistoryCliTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"LUMA_CONTROL_URL": "https://control.example.com", "LUMA_DEPLOY_TOKEN": "token"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *args):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from luma.cli import main
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--no-env", *args])
        return code, out.getvalue(), err.getvalue()

    def test_service_history_passes_filters_and_keeps_pagination_in_json(self):
        with patch("luma.cli.ControlClient") as factory:
            client = factory.return_value
            result = {"items": [], "page": {"limit": 5, "hasMore": True, "nextCursor": "next"}}
            client.history.return_value = result
            code, out, err = self.run_cli("service", "history", "api", "--kind", "deployment", "--status", "failed", "--source", "cli", "--since", "100", "--limit", "5", "--cursor", "previous", "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out)["result"], result)
        client.history.assert_called_once_with(query={"app": "api", "kind": "deployment", "status": "failed", "source": "cli", "since": "100", "limit": 5, "cursor": "previous"})

    def test_text_page_notice_is_on_stderr(self):
        with patch("luma.cli.ControlClient") as factory:
            factory.return_value.history.return_value = {"items": [], "page": {"hasMore": True, "nextCursor": "abc_def"}}
            code, out, err = self.run_cli("service", "history")
        self.assertEqual(code, 0)
        self.assertNotIn("abc_def", out)
        self.assertIn("--cursor abc_def", err)

    def test_history_detail_uses_kind_and_scoped_step_cursor(self):
        with patch("luma.cli.ControlClient") as factory:
            factory.return_value.history_detail.return_value = {"item": {"id": "deploy-x"}, "events": [], "page": {"hasMore": False}}
            code, out, err = self.run_cli("service", "history", "--id", "deploy-x", "--kind", "deployment", "--cursor", "step-page", "--limit", "2", "--format", "json")
            factory.return_value.history_detail.assert_called_once_with("deployment", "deploy-x", query={"cursor": "step-page", "limit": 2})
        self.assertEqual(code, 0)
        for arguments in (("--id", "x"), ("api", "--id", "x", "--kind", "build"), ("--limit", "101")):
            with patch("luma.cli.ControlClient") as factory:
                code, _, _ = self.run_cli("service", "history", *arguments)
                factory.assert_not_called()
            self.assertEqual(code, 1)

    def test_build_list_and_logs_page_through_existing_response_fields(self):
        with patch("luma.cli.ControlClient") as factory:
            factory.return_value.list_builds.return_value = {"runs": [], "page": {"hasMore": False}}
            code, out, _ = self.run_cli("build", "list", "--app", "api", "--limit", "2", "--format", "json")
            factory.return_value.list_builds.assert_called_once_with(query={"app": "api", "limit": 2})
            self.assertEqual(json.loads(out)["result"]["runs"], [])
            factory.return_value.get_build.return_value = {"run": {"id": "x", "events": []}, "eventsPage": {"hasMore": True, "nextCursor": "next-steps"}}
            code, out, err = self.run_cli("build", "logs", "x", "--limit", "3", "--cursor", "steps")
            factory.return_value.get_build.assert_called_once_with("x", query={"limit": 3, "cursor": "steps"})
            self.assertIn("--cursor next-steps", err)
        self.assertEqual(code, 0)

    def test_text_step_details_explain_retention_expiry(self):
        with patch("luma.cli.ControlClient") as factory:
            item = {"id": "expired", "detailsExpiredAt": 123456, "detailsRetentionDays": 30}
            factory.return_value.history_detail.return_value = {"item": item, "events": [], "page": {"hasMore": False}}
            code, out, err = self.run_cli("service", "history", "--id", "expired", "--kind", "build")
            self.assertEqual(code, 0)
            self.assertIn("Step log expired", out)
            self.assertIn("30-day retention", out)
            factory.return_value.get_build.return_value = {"run": {**item, "events": []}}
            code, out, err = self.run_cli("build", "logs", "expired")
            self.assertEqual(code, 0)
            self.assertIn("Step log expired", out)

    def test_nomad_history_parser_is_unchanged(self):
        from luma.cli import build_parser
        args = build_parser().parse_args(["history", "api"])
        self.assertEqual(args.command, "history")
        self.assertEqual(args.name, "api")
        self.assertFalse(hasattr(args, "cursor"))

    def test_client_encodes_history_cursor_and_ids(self):
        from luma.control.client import ControlClient
        from urllib.parse import parse_qs, urlparse
        client = ControlClient("https://control.example.com", "token")
        with patch.object(client, "request", return_value={}) as request:
            client.history_detail("deployment", "x/y", query={"cursor": "a&kind=build", "limit": 2})
        parsed = urlparse(request.call_args.args[1])
        self.assertEqual(parsed.path, "/v1/history/deployment/x%2Fy")
        self.assertEqual(parse_qs(parsed.query), {"cursor": ["a&kind=build"], "limit": ["2"]})
