from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from lae_cli.__main__ import main
from lae_cli.client import ApiClient, _NoRedirect
from lae_cli.config import DeployCredential, api_url, deploy_credential
from lae_cli.errors import CliError
from lae_cli.watch import watch_operation


TOKEN = "lae_dt_0123456789_" + ("A" * 43)


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class CliTests(unittest.TestCase):
    def test_url_and_token_inputs_are_safe(self) -> None:
        self.assertEqual(
            api_url({"LAE_API_URL": "http://127.0.0.1:8080/v1"}),
            "http://127.0.0.1:8080/v1",
        )
        for invalid in (
            "http://api.example.test/v1",
            "https://user:pass@api.example.test/v1",
            "https://api.example.test/v2",
            "https://api.example.test/v1?token=x",
        ):
            with self.subTest(url=invalid), self.assertRaises(CliError):
                api_url({"LAE_API_URL": invalid})
        credential = deploy_credential(
            from_stdin=True, environ={}, stdin=io.StringIO(TOKEN + "\n")
        )
        self.assertNotIn(TOKEN, repr(credential))
        with self.assertRaises(CliError):
            deploy_credential(from_stdin=False, environ={})

    def test_http_client_sends_bearer_but_never_reprs_it(self) -> None:
        captured = {}

        def opener(request, **_kwargs):
            captured["authorization"] = request.get_header("Authorization")
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["idempotency"] = request.get_header("Idempotency-key")
            captured["body"] = request.data
            return Response(b'{"status":"ok"}')

        client = ApiClient(
            "https://api.example.test/v1",
            DeployCredential(TOKEN),
            opener=opener,
        )
        self.assertEqual(
            client.get("/operations", query={"after": 7}), {"status": "ok"}
        )
        self.assertEqual(captured["authorization"], "Bearer " + TOKEN)
        self.assertEqual(
            captured["url"], "https://api.example.test/v1/operations?after=7"
        )
        self.assertEqual(
            client.patch(
                "/applications/app_test/environment",
                {"expectedVersion": 1, "set": {}, "unset": []},
                idempotency_key="env-patch-test",
            ),
            {"status": "ok"},
        )
        self.assertEqual(captured["method"], "PATCH")
        self.assertEqual(captured["idempotency"], "env-patch-test")
        self.assertIn(b'"expectedVersion":1', captured["body"])
        self.assertEqual(
            client.delete(
                "/source-connections/conn_test",
                idempotency_key="source-delete-test",
            ),
            {"status": "ok"},
        )
        self.assertEqual(captured["method"], "DELETE")
        self.assertEqual(captured["idempotency"], "source-delete-test")
        self.assertNotIn(TOKEN, repr(client))
        with self.assertRaises(CliError):
            client.get("/../secrets")

    def test_http_error_is_stable_and_does_not_echo_upstream_body(self) -> None:
        secret = "upstream-secret-must-not-escape"

        def opener(request, **_kwargs):
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "unauthorized",
                {},
                io.BytesIO(
                    json.dumps(
                        {
                            "error": {
                                "code": "LAE_UNAUTHENTICATED",
                                "message": secret,
                            }
                        }
                    ).encode()
                ),
            )

        client = ApiClient(
            "https://api.example.test/v1",
            DeployCredential(TOKEN),
            opener=opener,
        )
        with self.assertRaises(CliError) as caught:
            client.get("/me")
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_upload_verification_http_error_uses_source_failure_exit(self) -> None:
        def opener(request, **_kwargs):
            raise urllib.error.HTTPError(
                request.full_url,
                422,
                "invalid",
                {},
                io.BytesIO(
                    b'{"error":{"code":"LAE_UPLOAD_VERIFICATION_FAILED",'
                    b'"message":"signed-url-canary"}}'
                ),
            )

        client = ApiClient(
            "https://api.example.test/v1",
            DeployCredential(TOKEN),
            opener=opener,
        )
        with self.assertRaises(CliError) as caught:
            client.post(
                "/uploads/upl_test/complete",
                {},
                idempotency_key="upload-complete-test",
            )
        self.assertEqual(caught.exception.exit_code, 7)
        self.assertEqual(str(caught.exception), "Source validation failed.")
        self.assertNotIn("signed-url-canary", str(caught.exception))

    def test_http_client_never_forwards_bearer_across_redirects(self) -> None:
        request = urllib.request.Request(
            "https://api.example.test/v1/me",
            headers={"Authorization": "Bearer " + TOKEN},
        )
        redirected = _NoRedirect().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://attacker.example.test/collect",
        )
        self.assertIsNone(redirected)

    def test_watch_resumes_monotonic_cursor_and_returns_terminal_snapshot(self) -> None:
        class FakeClient:
            calls = []

            def get(self, path, *, query=None):
                self.calls.append((path, query))
                if path.endswith("/events"):
                    if query["after"] == 4:
                        return {
                            "events": [
                                {
                                    "operationId": "op_test",
                                    "cursor": 5,
                                    "type": "builder.analyze.progress",
                                    "phase": "source.analyze",
                                    "status": "running",
                                    "message": "Source analysis updated",
                                    "credentialLeaseId": "must-be-dropped",
                                }
                            ],
                            "status": "running",
                            "terminal": False,
                        }
                    return {"events": [], "status": "succeeded", "terminal": True}
                return {"id": "op_test", "status": "succeeded"}

        events = []
        client = FakeClient()
        result = watch_operation(
            client,  # type: ignore[arg-type]
            "op_test",
            after=4,
            poll_seconds=0,
            on_event=events.append,
            sleeper=lambda _delay: None,
        )
        self.assertEqual(result.cursor, 5)
        self.assertEqual(result.status, "succeeded")
        self.assertNotIn("credentialLeaseId", events[0])
        self.assertEqual(client.calls[1][1]["after"], 5)

    def test_watch_drains_all_pages_before_terminal_snapshot(self) -> None:
        class FakeClient:
            calls = []

            def get(self, path, *, query=None):
                self.calls.append((path, query))
                if path.endswith("/events"):
                    if query["after"] == 0:
                        return {
                            "events": [
                                {
                                    "operationId": "op_test",
                                    "cursor": 1,
                                    "type": "operation.started",
                                    "status": "running",
                                }
                            ],
                            "status": "succeeded",
                            "terminal": False,
                            "hasMore": True,
                        }
                    return {
                        "events": [
                            {
                                "operationId": "op_test",
                                "cursor": 2,
                                "type": "operation.succeeded",
                                "status": "succeeded",
                            }
                        ],
                        "status": "succeeded",
                        "terminal": True,
                        "hasMore": False,
                    }
                return {"id": "op_test", "status": "succeeded"}

        result = watch_operation(
            FakeClient(),  # type: ignore[arg-type]
            "op_test",
            poll_seconds=0,
            sleeper=lambda _delay: None,
        )
        self.assertEqual(result.cursor, 2)
        self.assertEqual(result.status, "succeeded")

    def test_existing_smoke_commands_and_missing_auth_exit_are_stable(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["--format", "json", "version"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["component"], "lae-cli")

        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr):
            self.assertEqual(main(["--format", "json", "whoami"]), 3)
        self.assertEqual(
            json.loads(stderr.getvalue())["error"]["code"], "LAE_UNAUTHENTICATED"
        )

    def test_token_cannot_be_passed_as_a_command_argument(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(main(["--token", TOKEN, "whoami"]), 2)
        self.assertNotIn(TOKEN, stderr.getvalue())
        self.assertNotIn(TOKEN, repr(_parser_actions()))

    def test_global_format_is_accepted_after_a_command(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr):
            self.assertEqual(main(["whoami", "--format", "json"]), 3)
        self.assertEqual(
            json.loads(stderr.getvalue())["error"]["code"], "LAE_UNAUTHENTICATED"
        )

    def test_invalid_arguments_return_stable_json_without_echoing_values(self) -> None:
        canary = "argument-canary-must-not-be-echoed"
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(
                main(["whoami", "--format", "json", "--unknown", canary]), 2
            )
        error = json.loads(stderr.getvalue())["error"]
        self.assertEqual(error["code"], "LAE_CLI_ARGUMENT_INVALID")
        self.assertNotIn(canary, stderr.getvalue())

    def test_inspect_posts_only_a_credential_free_git_source(self) -> None:
        class FakeClient:
            calls = []

            def post(self, path, body=None, *, idempotency_key=None):
                self.calls.append((path, body, idempotency_key))
                return {
                    "analysis": {"id": "ana_test", "status": "queued"},
                    "operation": {"id": "op_test", "status": "queued"},
                }

        client = FakeClient()
        stdout = io.StringIO()
        with patch("lae_cli.__main__._client", return_value=client), redirect_stdout(
            stdout
        ):
            exit_code = main(
                [
                    "inspect",
                    "--app",
                    "app_test",
                    "--repo",
                    "https://git.example.test/acme/app.git",
                    "--ref",
                    "0123456789abcdef0123456789abcdef01234567",
                    "--connection-id",
                    "conn_test",
                    "--idempotency-key",
                    "inspect-test-1",
                    "--no-wait",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(exit_code, 0)
        path, body, key = client.calls[0]
        self.assertEqual(path, "/analyses")
        self.assertEqual(key, "inspect-test-1")
        self.assertEqual(body["applicationId"], "app_test")
        self.assertEqual(body["intent"]["publicProtocols"], ["http"])
        self.assertEqual(body["source"]["connectionId"], "conn_test")
        self.assertNotIn("credential", json.dumps(body).lower())

        for index, repository in enumerate(
            (
                "https://user:password@git.example.test/acme/app.git",
                "https://127.0.0.1/acme/app.git",
                "https://[::1]/acme/app.git",
                "https://localhost/acme/app.git",
                "https://git.internal/acme/app.git",
            ),
            start=2,
        ):
            stderr = io.StringIO()
            with (
                self.subTest(repository=repository),
                patch("lae_cli.__main__._client", return_value=client),
                redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "inspect",
                        "--app",
                        "app_test",
                        "--repo",
                        repository,
                        "--ref",
                        "main",
                        "--idempotency-key",
                        f"inspect-test-{index}",
                        "--no-wait",
                        "--format",
                        "json",
                    ]
                )
            self.assertEqual(exit_code, 2)
            self.assertNotIn("password", stderr.getvalue())

    def test_public_commands_reject_internal_home_region_before_api_call(self) -> None:
        class FakeClient:
            calls = []

        commands = (
            [
                "inspect",
                "--app",
                "app_test",
                "--repo",
                "https://git.example.test/acme/app.git",
                "--ref",
                "main",
                "--region",
                "home",
                "--idempotency-key",
                "region-git",
                "--no-wait",
            ],
            [
                "templates",
                "launch",
                "fastapi-minimal",
                "--name",
                "Home",
                "--slug",
                "home",
                "--region",
                "home",
                "--idempotency-key",
                "region-template",
            ],
        )
        for argv in commands:
            with (
                self.subTest(command=argv[0]),
                patch("lae_cli.__main__._client", return_value=FakeClient()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(argv), 2)
        self.assertEqual(FakeClient.calls, [])

    def test_config_show_and_inspect_emit_only_configuration_schema(self) -> None:
        canary = "configuration-value-must-not-escape"
        configuration = {
            "configuration": {
                "sourceRevisionId": "src_test",
                "kind": "compose",
                "serviceKeys": ["web", "worker"],
                "environmentSchemaDigest": "sha256:" + ("a" * 64),
                "environment": [
                    {
                        "name": "DATABASE_URL",
                        "serviceKeys": ["web", "worker"],
                        "required": True,
                        "sensitive": True,
                        "value": canary,
                    }
                ],
                "secret": canary,
            }
        }

        class FakeClient:
            calls = []

            def post(self, path, body=None, *, idempotency_key=None):
                self.calls.append(("POST", path, body, idempotency_key))
                return {
                    "analysis": {"id": "ana_test", "status": "queued"},
                    "operation": {"id": "op_test", "status": "queued"},
                }

            def get(self, path, *, query=None):
                self.calls.append(("GET", path, query))
                if path == "/analyses/ana_test":
                    return {"id": "ana_test", "status": "needs_configuration"}
                return configuration

        client = FakeClient()
        stdout = io.StringIO()
        with patch("lae_cli.__main__._client", return_value=client), redirect_stdout(
            stdout
        ):
            code = main(
                [
                    "config",
                    "show",
                    "--app",
                    "app_test",
                    "--analysis",
                    "ana_test",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertNotIn(canary, stdout.getvalue())
        rendered = json.loads(stdout.getvalue())["configuration"]
        self.assertEqual(rendered["environment"][0]["serviceKeys"], ["web", "worker"])
        self.assertNotIn("value", rendered["environment"][0])

        stdout = io.StringIO()
        with (
            patch("lae_cli.__main__._client", return_value=client),
            patch(
                "lae_cli.__main__._watch",
                return_value=SimpleNamespace(
                    status="succeeded",
                    cursor=3,
                    operation={"id": "op_test", "status": "succeeded"},
                ),
            ),
            redirect_stdout(stdout),
        ):
            code = main(
                [
                    "inspect",
                    "--app",
                    "app_test",
                    "--repo",
                    "https://git.example.test/acme/app.git",
                    "--ref",
                    "main",
                    "--idempotency-key",
                    "inspect-config-test",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(code, 4)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["configuration"]["environment"][0]["name"], "DATABASE_URL")
        self.assertNotIn(canary, stdout.getvalue())
        self.assertIn(
            (
                "GET",
                "/applications/app_test/analyses/ana_test/configuration",
                None,
            ),
            client.calls,
        )

    def test_deploy_uses_analysis_and_environment_version_contract(self) -> None:
        class FakeClient:
            calls = []

            def post(self, path, body=None, *, idempotency_key=None):
                self.calls.append((path, body, idempotency_key))
                return {"operation": {"id": "op_deploy", "status": "queued"}}

        client = FakeClient()
        stdout = io.StringIO()
        with patch("lae_cli.__main__._client", return_value=client), redirect_stdout(
            stdout
        ):
            exit_code = main(
                [
                    "deploy",
                    "--app",
                    "app_test",
                    "--analysis",
                    "ana_test",
                    "--environment-version",
                    "7",
                    "--idempotency-key",
                    "deploy-test-1",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(exit_code, 0)
        path, body, key = client.calls[0]
        self.assertEqual(path, "/applications/app_test/deployments")
        self.assertEqual(key, "deploy-test-1")
        self.assertEqual(
            body,
            {
                "analysisId": "ana_test",
                "environmentVersion": 7,
            },
        )

    def test_apps_create_builds_a_pending_draft_request(self) -> None:
        class FakeClient:
            calls = []

            def post(self, path, body=None, *, idempotency_key=None):
                self.calls.append((path, body, idempotency_key))
                return {
                    "application": {
                        "id": "app_test",
                        "name": "Notes",
                        "slug": "notes",
                        "kind": "pending",
                    }
                }

        client = FakeClient()
        stdout = io.StringIO()
        with patch("lae_cli.__main__._client", return_value=client), redirect_stdout(
            stdout
        ):
            exit_code = main(
                [
                    "apps",
                    "create",
                    "--name",
                    "Notes",
                    "--slug",
                    "notes",
                    "--idempotency-key",
                    "app-create-test-1",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            client.calls[0],
            (
                "/applications",
                {"name": "Notes", "slug": "notes"},
                "app-create-test-1",
            ),
        )
        self.assertEqual(json.loads(stdout.getvalue())["application"]["kind"], "pending")

    def test_apps_lifecycle_uses_scoped_actions_and_delete_requires_confirmation(self) -> None:
        class FakeClient:
            calls = []

            def post(self, path, body=None, *, idempotency_key=None):
                self.calls.append((path, body, idempotency_key))
                return {"operation": {"id": "op_action", "status": "queued"}}

        client = FakeClient()
        stderr = io.StringIO()
        with patch("lae_cli.__main__._client", return_value=client), redirect_stdout(
            io.StringIO()
        ), redirect_stderr(stderr):
            self.assertEqual(
                main(
                    [
                        "apps",
                        "rollback",
                        "app_test",
                        "--deployment",
                        "dep_previous",
                        "--idempotency-key",
                        "rollback-test-1",
                    ]
                ),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "apps",
                        "delete",
                        "app_test",
                        "--idempotency-key",
                        "delete-test-1",
                    ]
                ),
                2,
            )
            self.assertEqual(
                main(
                    [
                        "apps",
                        "delete",
                        "app_test",
                        "--yes",
                        "--idempotency-key",
                        "delete-test-2",
                    ]
                ),
                0,
            )
        self.assertIn("LAE_CLI_CONFIRMATION_REQUIRED", stderr.getvalue())
        self.assertEqual(
            client.calls[0],
            (
                "/applications/app_test/actions/rollback",
                {"deploymentId": "dep_previous"},
                "rollback-test-1",
            ),
        )
        self.assertEqual(
            client.calls[1],
            (
                "/applications/app_test/actions/delete",
                {},
                "delete-test-2",
            ),
        )

    def test_apps_logs_and_metrics_are_tenant_scoped_queries(self) -> None:
        class FakeClient:
            calls = []

            def get(self, path, *, query=None):
                self.calls.append((path, query))
                return {"applicationId": "app_test", "serviceKey": "web"}

        client = FakeClient()
        with patch("lae_cli.__main__._client", return_value=client), redirect_stdout(
            io.StringIO()
        ):
            self.assertEqual(
                main(
                    [
                        "apps",
                        "logs",
                        "app_test",
                        "--service",
                        "web",
                        "--tail",
                        "250",
                    ]
                ),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "apps",
                        "metrics",
                        "app_test",
                        "--window",
                        "7200",
                    ]
                ),
                0,
            )
        self.assertEqual(
            client.calls,
            [
                ("/applications/app_test/logs", {"tail": 250, "service": "web"}),
                ("/applications/app_test/metrics", {"window": 7200}),
            ],
        )

    def test_apps_deployments_returns_bounded_server_history(self) -> None:
        class FakeClient:
            calls = []

            def get(self, path, *, query=None):
                self.calls.append((path, query))
                return {
                    "deployments": [
                        {"id": "dep_current", "status": "succeeded"}
                    ]
                }

        client = FakeClient()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("lae_cli.__main__._client", return_value=client), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            self.assertEqual(
                main(
                    [
                        "apps",
                        "deployments",
                        "app_test",
                        "--limit",
                        "7",
                        "--format",
                        "json",
                    ]
                ),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "apps",
                        "deployments",
                        "app_test",
                        "--limit",
                        "101",
                        "--format",
                        "json",
                    ]
                ),
                2,
            )
        self.assertEqual(
            client.calls,
            [("/applications/app_test/deployments", {"limit": 7})],
        )
        self.assertEqual(
            json.loads(stdout.getvalue())["deployments"][0]["id"],
            "dep_current",
        )
        self.assertIn("LAE_CLI_ARGUMENT_INVALID", stderr.getvalue())

    def test_template_launch_uses_curated_server_catalog(self) -> None:
        class FakeClient:
            calls = []

            def post(self, path, body=None, *, idempotency_key=None):
                self.calls.append((path, body, idempotency_key))
                return {
                    "template": {"id": "fastapi-minimal"},
                    "application": {"id": "app_template"},
                    "analysis": {"id": "ana_template", "status": "queued"},
                    "operation": {"id": "op_template", "status": "queued"},
                }

        client = FakeClient()
        with patch("lae_cli.__main__._client", return_value=client), redirect_stdout(
            io.StringIO()
        ):
            result = main(
                [
                    "templates",
                    "launch",
                    "fastapi-minimal",
                    "--name",
                    "Fast API Demo",
                    "--slug",
                    "fast-api-demo",
                    "--region",
                    "cn",
                    "--idempotency-key",
                    "template-launch-test-1",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(
            client.calls,
            [
                (
                    "/templates/fastapi-minimal/launch",
                    {"name": "Fast API Demo", "slug": "fast-api-demo", "region": "cn"},
                    "template-launch-test-1",
                )
            ],
        )

    def test_env_value_uses_stdin_and_response_never_echoes_plaintext(self) -> None:
        secret = "environment-canary-must-not-escape"

        class FakeClient:
            calls = []

            def patch(self, path, body, *, idempotency_key=None):
                self.calls.append((path, body, idempotency_key))
                return {
                    "version": 8,
                    "environment": [
                        {
                            "name": "DATABASE_PASSWORD",
                            "configured": True,
                            "value": secret,
                            "valueCiphertext": secret,
                        }
                    ],
                }

        client = FakeClient()
        stdout = io.StringIO()
        with (
            patch("lae_cli.__main__._client", return_value=client),
            patch("sys.stdin", io.StringIO(secret)),
            redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "env",
                    "set",
                    "app_test",
                    "DATABASE_PASSWORD",
                    "--service",
                    "db",
                    "--expected-version",
                    "7",
                    "--value-stdin",
                    "--idempotency-key",
                    "env-test-1",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(exit_code, 0)
        path, body, key = client.calls[0]
        self.assertEqual(path, "/applications/app_test/environment")
        self.assertEqual(key, "env-test-1")
        self.assertEqual(body["set"]["db:DATABASE_PASSWORD"]["value"], secret)
        self.assertTrue(body["set"]["db:DATABASE_PASSWORD"]["sensitive"])
        self.assertNotIn(secret, stdout.getvalue())

    def test_checkout_only_creates_a_user_confirmed_session(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = []

            def post(self, path, body=None, *, idempotency_key=None):
                self.calls.append((path, body, idempotency_key))
                return {
                    "orderId": "order_test",
                    "checkoutUrl": "https://pay.example.test/session",
                    "requiresUserAction": True,
                }

        for cli_interval, api_interval in (
            ("month", "monthly"),
            ("year", "yearly"),
        ):
            with self.subTest(interval=cli_interval):
                client = FakeClient()
                stdout = io.StringIO()
                with patch(
                    "lae_cli.__main__._client", return_value=client
                ), redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "billing",
                            "checkout",
                            "--plan",
                            "pro",
                            "--interval",
                            cli_interval,
                            "--idempotency-key",
                            "checkout-test-1",
                            "--format",
                            "json",
                        ]
                    )
                self.assertEqual(exit_code, 0)
                self.assertEqual(
                    client.calls[0],
                    (
                        "/billing/checkout-sessions",
                        {"plan": "pro", "interval": api_interval},
                        "checkout-test-1",
                    ),
                )
                self.assertTrue(
                    json.loads(stdout.getvalue())["requiresUserAction"]
                )

    def test_checkout_rejects_client_selected_provider(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = main(
                [
                    "billing",
                    "checkout",
                    "--plan",
                    "pro",
                    "--interval",
                    "year",
                    "--provider",
                    "mock",
                    "--idempotency-key",
                    "checkout-test-1",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(
            json.loads(stderr.getvalue())["error"]["code"],
            "LAE_CLI_ARGUMENT_INVALID",
        )


def _parser_actions() -> list[str]:
    # The public help is enough to enforce that no plaintext token option is
    # accidentally added. Avoid importing private parser internals in callers.
    stdout = io.StringIO()
    try:
        with redirect_stdout(stdout):
            main(["--help"])
    except SystemExit:
        pass
    return stdout.getvalue().splitlines()


if __name__ == "__main__":
    unittest.main()
