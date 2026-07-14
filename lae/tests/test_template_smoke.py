from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "template_smoke", SCRIPTS / "template_smoke.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class _Response:
    def __init__(self, body):
        self.body = body


class _Client:
    def __init__(self, templates):
        self.templates = templates


class TemplateSmokeTests(unittest.TestCase):
    def test_catalog_selection_rejects_missing_template(self) -> None:
        client = _Client([{"id": "fastapi-minimal", "version": "v1"}])
        with patch.object(
            MODULE,
            "request_with_retry",
            return_value=_Response({"templates": client.templates}),
        ):
            with self.assertRaisesRegex(
                MODULE.AcceptanceFailure, "requested template is not available"
            ):
                MODULE._template_ids(
                    client, {"missing"}, deadline=100.0
                )

    def test_catalog_selection_preserves_public_order(self) -> None:
        client = _Client(
            [
                {"id": "nextjs-docker", "version": "v1"},
                {"id": "fastapi-minimal", "version": "v2"},
            ]
        )
        with patch.object(
            MODULE,
            "request_with_retry",
            return_value=_Response({"templates": client.templates}),
        ):
            self.assertEqual(
                MODULE._template_ids(client, None, deadline=100.0),
                [("nextjs-docker", "v1"), ("fastapi-minimal", "v2")],
            )

    def test_route_probe_accepts_redirect_or_success_only(self) -> None:
        class HttpResponse:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b""

        with patch.object(MODULE.urllib.request, "urlopen", return_value=HttpResponse()):
            self.assertEqual(
                MODULE._probe_route(
                    "example.test", deadline=MODULE.time.monotonic() + 1, timeout_seconds=1
                ),
                204,
            )

    def test_reporting_outage_does_not_become_a_template_failure(self) -> None:
        args = SimpleNamespace(
            api_base="https://api.example.test/v1",
            report_api_base="https://api.example.test",
            request_timeout=1,
            timeout_seconds=1,
            templates=None,
            keep_failed=False,
            fail_fast=False,
        )
        report_calls = []

        def request(*call_args, **call_kwargs):
            report_calls.append((call_args, call_kwargs))
            raise MODULE.ApiFailure(503, "LAE_API_UNAVAILABLE", False)

        with (
            patch.dict(
                MODULE.os.environ,
                {
                    "LAE_DEPLOY_TOKEN": "lae_dt_test",
                    "LAE_TEMPLATE_SMOKE_REPORT_TOKEN": "s" * 48,
                },
            ),
            patch.object(MODULE, "_template_ids", return_value=[("fastapi-minimal", "v1")]),
            patch.object(
                MODULE,
                "smoke_one",
                return_value={"templateId": "fastapi-minimal", "status": "succeeded"},
            ),
            patch.object(MODULE, "request_with_retry", side_effect=request),
        ):
            with self.assertRaises(MODULE.ApiFailure):
                MODULE.run(args)
        self.assertEqual(len(report_calls), 1)

    def test_scheduler_failure_is_not_reported_as_template_failure(self) -> None:
        args = SimpleNamespace(
            api_base="https://api.example.test/v1",
            report_api_base="https://api.example.test",
            request_timeout=1,
            timeout_seconds=1,
            templates=None,
            keep_failed=False,
            fail_fast=False,
        )
        with (
            patch.dict(
                MODULE.os.environ,
                {
                    "LAE_DEPLOY_TOKEN": "lae_dt_test",
                    "LAE_TEMPLATE_SMOKE_REPORT_TOKEN": "s" * 48,
                },
            ),
            patch.object(MODULE, "_template_ids", return_value=[("fastapi-minimal", "v1")]),
            patch.object(
                MODULE,
                "smoke_one",
                side_effect=MODULE.SchedulerFailure("cleanup failed"),
            ),
            patch.object(MODULE, "request_with_retry") as request,
        ):
            with self.assertRaises(MODULE.SchedulerFailure):
                MODULE.run(args)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
