import unittest
from contextlib import ExitStack
from unittest.mock import patch

from luma.config import LumaConfig
from luma.control import server
from luma.errors import LumaError


class DashboardScopeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        state = {"deployToken": "token", "joinToken": "join", "nodes": {}}
        self.stack.enter_context(patch.object(server, "load_dashboard_state", return_value=state))
        self.stack.enter_context(patch.object(server, "load_config", return_value=LumaConfig({"defaults": {}}, None)))
        self.status = self.stack.enter_context(patch.object(server, "nomad_status_summary", return_value={"available": True, "nodes": []}))
        self.services = self.stack.enter_context(patch.object(server, "nomad_services_summary", return_value=[]))
        self.stats = self.stack.enter_context(patch.object(server, "_service_stats_by_name", return_value={}))
        self.routes = self.stack.enter_context(patch.object(server, "_dashboard_route_files", return_value={}))

    def test_light_scopes_do_not_query_services_stats_or_routes(self):
        for scope in ("nodes", "setup"):
            with self.subTest(scope=scope):
                result = server.handle_dashboard("token", scope=scope)
                self.assertIn("readiness", result)
                self.assertNotIn("services", result)
                self.assertNotIn("storage", result)
                self.assertEqual("nodes" in result, scope == "nodes")
                self.assertEqual("nodeJoin" in result, scope == "nodes")
        self.services.assert_not_called()
        self.stats.assert_not_called()
        self.routes.assert_not_called()
        self.assertEqual(self.status.call_count, 2)

    def test_default_preserves_full_contract(self):
        result = server.handle_dashboard("token")
        for field in ("services", "nodes", "trafficPaths", "storage", "issues", "nodeJoin", "build"):
            self.assertIn(field, result)
        self.services.assert_called_once()
        self.stats.assert_called_once()
        self.routes.assert_called_once()

    def test_bad_scope_and_bad_auth_do_no_upstream_work(self):
        for token, scope in (("wrong", "nodes"), ("token", "unknown")):
            with self.assertRaises(LumaError):
                server.handle_dashboard(token, scope=scope)
        self.status.assert_not_called()
        self.services.assert_not_called()

    def test_directory_never_enriches_jobs_or_advertises_health(self):
        with patch.object(server, "nomad_service_directory", return_value=[{"name": "app", "jobId": "app"}]) as directory:
            result = server.handle_dashboard("token", scope="directory")
        directory.assert_called_once()
        self.services.assert_not_called()
        self.stats.assert_not_called()
        self.status.assert_not_called()
        self.routes.assert_not_called()
        self.assertEqual(result["services"], [{"name": "app", "stack": "app", "managedBy": ""}])
        self.assertNotIn("issues", result)
        self.assertNotIn("readiness", result)

    def test_application_queries_only_selected_job_and_keeps_details(self):
        service = {"name": "web", "stack": "app", "tasks": [{"id": "allocation"}], "storage": [{"name": "data"}]}
        with patch.object(server, "_dashboard_nomad_services", return_value=[service]):
            result = server.handle_dashboard("token", scope="application", app="app")
        self.assertEqual(self.services.call_args.kwargs, {"job_ids": {"app"}})
        self.assertEqual(self.stats.call_args.kwargs["job_id"], "app")
        self.assertEqual(result["services"][0]["storage"], [{"name": "data"}])
        self.assertEqual(result["services"][0]["tasks"], [{"id": "allocation"}])
        self.assertNotIn("trafficPaths", result)

    def test_list_paginates_apps_not_services_and_filters_whole_inventory(self):
        services = [{"name": f"app-{n:03}", "stack": f"app-{n:03}", "region": "cn", "status": "running", "image": f"image-{n}", "tasks": [{"error": "large log"}], "storage": [{"name": "data"}]} for n in range(62)]
        services.append({**services[-1], "name": "worker"})
        services.append({"name": "traefik", "status": "running"})
        with patch.object(server, "_dashboard_nomad_services", return_value=services):
            result = server.handle_dashboard("token", scope="applications", query={"offset": "50", "limit": "50"})
            filtered = server.handle_dashboard("token", scope="applications", query={"q": "image-61"})
        self.assertEqual(result["applicationPage"]["total"], 62)
        self.assertEqual(result["applicationPage"]["counts"]["total"], 62)
        self.assertEqual(len(result["services"]), 13)
        self.assertFalse(result["applicationPage"]["hasMore"])
        self.assertEqual(filtered["applicationPage"]["total"], 1)
        self.assertEqual(len(filtered["services"]), 2)
        self.assertNotIn("tasks", result["services"][0])
        self.assertNotIn("storage", result["services"][0])
        self.status.assert_not_called()
        self.stats.assert_not_called()

    def test_overview_and_fleet_strip_task_details_but_keep_health_and_placement(self):
        service = {"name": "app", "stack": "app", "failed": 1, "tasks": [{"node": "worker", "error": "large error"}], "nodes": ["worker"]}
        with patch.object(server, "_dashboard_nomad_services", return_value=[service]):
            overview = server.handle_dashboard("token", scope="overview")
            fleet = server.handle_dashboard("token", scope="fleet")
        self.assertEqual(overview["services"][0]["failed"], 1)
        self.assertTrue(overview["issues"])
        self.assertNotIn("tasks", overview["services"][0])
        self.assertEqual(fleet["services"][0]["tasks"], [{"node": "worker"}])
        self.stats.assert_called_once()
        self.routes.assert_not_called()

    def test_sync_and_asgi_routes_pass_scope_app_and_pagination(self):
        import asyncio
        import json
        from unittest.mock import Mock
        query = "scope=applications&offset=50&limit=25&q=a%26b"
        with patch.object(server, "handle_dashboard", return_value={"scope": "applications"}) as dashboard:
            handler = server.ControlHandler.__new__(server.ControlHandler)
            handler.path = "/v1/dashboard?" + query
            handler.headers = {"Authorization": "Bearer token"}
            handler._json = Mock()
            handler.do_GET()
            legacy_kwargs = dashboard.call_args.kwargs
            request = server.Request({"type": "http", "method": "GET", "path": "/v1/dashboard", "query_string": query.encode(), "headers": [(b"authorization", b"Bearer token")]})
            response = asyncio.run(server._asgi_authenticated_get(request))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.body), {"scope": "applications"})
            self.assertEqual(dashboard.call_args.kwargs, legacy_kwargs)
            self.assertEqual(legacy_kwargs["query"]["q"], "a&b")
            self.assertEqual(legacy_kwargs["query"]["offset"], "50")
            self.assertEqual(legacy_kwargs["scope"], "applications")

    def test_overview_preserves_service_memory_diagnostic(self):
        service = {"name": "app", "fullName": "app", "stack": "app", "status": "running", "running": 1, "desired": 1}
        self.stats.return_value = {"app": [{"memoryUsageBytes": 91, "memoryLimitBytes": 100}]}
        with patch.object(server, "_dashboard_nomad_services", side_effect=lambda *args, **kwargs: [dict(service)]):
            full = server.handle_dashboard("token")
            overview = server.handle_dashboard("token", scope="overview")
        full_memory = [item for item in full["issues"] if item["kind"] == "service-memory"]
        self.assertTrue(full_memory)
        self.assertEqual(full_memory, [item for item in overview["issues"] if item["kind"] == "service-memory"])
