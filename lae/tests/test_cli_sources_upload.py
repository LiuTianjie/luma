from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import urllib.error
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lae_cli.__main__ import _wait_upload_ready, main
from lae_cli.errors import CliError
from lae_cli.upload import MAX_UPLOAD_BYTES, open_local_upload, put_upload_transfer


class Response(io.BytesIO):
    def __init__(self, body: bytes = b"", status: int = 200) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def upload_body(
    upload_id: str,
    *,
    filename: str,
    media_type: str,
    size_bytes: int,
    digest: str,
    status: str,
    failure_code: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "id": upload_id,
        "applicationId": "app_test",
        "filename": filename,
        "kind": "html" if filename.lower().endswith(".html") else "zip",
        "mediaType": media_type,
        "expectedBytes": size_bytes,
        "actualBytes": size_bytes if status in {"scanning", "ready"} else None,
        "sha256": digest,
        "status": status,
        "cleanupStatus": "none",
        "sourceRevisionId": "src_test" if status == "ready" else None,
    }
    if failure_code is not None:
        body["failureCode"] = failure_code
    return body


class SourceConnectionCliTests(unittest.TestCase):
    def test_create_rotate_list_and_revoke_keep_secrets_off_output(self) -> None:
        secret = "git-secret-canary-must-not-escape"

        class FakeClient:
            calls: list[tuple[object, ...]] = []

            def get(self, path, *, query=None):
                self.calls.append(("GET", path, query))
                return {
                    "connections": [
                        {
                            "id": "conn_test",
                            "displayName": "Private Git",
                            "secret": secret,
                            "credentialVersion": 1,
                        }
                    ]
                }

            def post(self, path, body=None, *, idempotency_key=None):
                self.calls.append(("POST", path, body, idempotency_key))
                return {
                    "connection": {
                        "id": "conn_test",
                        "displayName": "Private Git",
                        "credentialVersion": 2 if path.endswith("rotate") else 1,
                        "secret": secret,
                        "ciphertext": secret,
                    }
                }

            def delete(self, path, *, idempotency_key):
                self.calls.append(("DELETE", path, idempotency_key))
                return {}

        client = FakeClient()
        stdout = io.StringIO()
        with (
            patch("lae_cli.__main__._client", return_value=client),
            patch("sys.stdin", io.StringIO(secret + "\n")),
            redirect_stdout(stdout),
        ):
            code = main(
                [
                    "source-connections",
                    "create",
                    "--provider",
                    "gitea",
                    "--name",
                    "Private Git",
                    "--base-url",
                    "https://git.example.com/gitea/",
                    "--username",
                    "deploy",
                    "--secret-stdin",
                    "--idempotency-key",
                    "source-create-test",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(code, 0)
        create_call = client.calls[-1]
        self.assertEqual(create_call[1], "/source-connections")
        self.assertEqual(create_call[2]["secret"], secret)
        self.assertEqual(create_call[2]["baseUrl"], "https://git.example.com/gitea")
        self.assertEqual(create_call[3], "source-create-test")
        self.assertNotIn(secret, stdout.getvalue())
        self.assertNotIn("ciphertext", stdout.getvalue().lower())

        stdout = io.StringIO()
        with (
            patch("lae_cli.__main__._client", return_value=client),
            patch("sys.stdin", io.StringIO(secret + "-v2\n")),
            redirect_stdout(stdout),
        ):
            code = main(
                [
                    "source-connections",
                    "rotate",
                    "conn_test",
                    "--secret-stdin",
                    "--idempotency-key",
                    "source-rotate-test",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(client.calls[-1][1], "/source-connections/conn_test/rotate")
        self.assertEqual(client.calls[-1][3], "source-rotate-test")
        self.assertNotIn(secret, stdout.getvalue())

        stdout = io.StringIO()
        with patch("lae_cli.__main__._client", return_value=client), redirect_stdout(
            stdout
        ):
            self.assertEqual(
                main(["source-connections", "list", "--format", "json"]), 0
            )
        self.assertNotIn(secret, stdout.getvalue())

        stdout = io.StringIO()
        with patch("lae_cli.__main__._client", return_value=client), redirect_stdout(
            stdout
        ):
            code = main(
                [
                    "source-connections",
                    "revoke",
                    "conn_test",
                    "--idempotency-key",
                    "source-revoke-test",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            client.calls[-1],
            ("DELETE", "/source-connections/conn_test", "source-revoke-test"),
        )
        self.assertEqual(json.loads(stdout.getvalue())["revoked"], True)

    def test_secret_stdin_conflicts_with_token_stdin_before_authentication(self) -> None:
        stderr = io.StringIO()
        with (
            patch("lae_cli.__main__._client", side_effect=AssertionError("must not run")),
            redirect_stderr(stderr),
        ):
            code = main(
                [
                    "source-connections",
                    "create",
                    "--provider",
                    "github",
                    "--name",
                    "GitHub",
                    "--base-url",
                    "https://github.com",
                    "--secret-stdin",
                    "--idempotency-key",
                    "stdin-conflict-test",
                    "--token-stdin",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(stderr.getvalue())["error"]["code"],
            "LAE_CLI_STDIN_CONFLICT",
        )

    def test_source_secret_and_base_url_errors_never_echo_input(self) -> None:
        canary = "private-source-canary"
        stderr = io.StringIO()
        with (
            patch("lae_cli.__main__._client", return_value=object()),
            patch("sys.stdin", io.StringIO(canary + "\nsecond-line")),
            redirect_stderr(stderr),
        ):
            code = main(
                [
                    "source-connections",
                    "create",
                    "--provider",
                    "generic",
                    "--name",
                    "Private Git",
                    "--base-url",
                    "https://user:password@git.example.com",
                    "--secret-stdin",
                    "--idempotency-key",
                    "source-invalid-test",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(code, 2)
        self.assertNotIn("password", stderr.getvalue())
        self.assertNotIn(canary, stderr.getvalue())


class StaticUploadCliTests(unittest.TestCase):
    def test_local_upload_hashes_stream_and_rejects_unsafe_paths(self) -> None:
        content = b"<!doctype html><title>Lake</title>"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "site.HTML"
            source_path.write_bytes(content)
            with open_local_upload(str(source_path)) as source:
                self.assertEqual(source.filename, "site.HTML")
                self.assertEqual(source.media_type, "text/html")
                self.assertEqual(source.size_bytes, len(content))
                self.assertEqual(
                    source.sha256, "sha256:" + hashlib.sha256(content).hexdigest()
                )
                self.assertEqual(source.rewind_verified().read(), content)
                self.assertNotIn(str(source_path), repr(source))

            symlink = root / "linked.html"
            symlink.symlink_to(source_path)
            directory_path = root / "folder.html"
            directory_path.mkdir()
            unsupported = root / "source.py"
            unsupported.write_text("print('no')", encoding="utf-8")
            for path in (symlink, directory_path):
                with self.subTest(path=path), self.assertRaises(CliError) as caught:
                    open_local_upload(str(path))
                self.assertEqual(caught.exception.exit_code, 2)
            with self.assertRaises(CliError) as caught:
                open_local_upload(str(unsupported))
            self.assertEqual(caught.exception.exit_code, 5)

            oversized = root / "large.zip"
            with oversized.open("wb") as stream:
                stream.truncate(MAX_UPLOAD_BYTES + 1)
            with self.assertRaises(CliError) as caught:
                open_local_upload(str(oversized))
            self.assertEqual(caught.exception.exit_code, 6)

    def test_transfer_streams_exact_headers_without_bearer_cookie_or_redirect(self) -> None:
        content = b"<h1>Still water</h1>"
        signed_url = "https://objects.example.com/once/file?X-Signature=private-canary"
        captured: dict[str, object] = {}

        def opener(request, **_kwargs):
            captured["method"] = request.get_method()
            captured["headers"] = {
                name.casefold(): value for name, value in request.header_items()
            }
            captured["body"] = request.data.read()
            return Response(status=200)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.html"
            path.write_bytes(content)
            with open_local_upload(str(path)) as source:
                put_upload_transfer(
                    {
                        "method": "PUT",
                        "url": signed_url,
                        "headers": {
                            "Content-Length": str(len(content)),
                            "Content-Type": "text/html",
                            "If-None-Match": "*",
                        },
                    },
                    source,
                    opener=opener,
                )
        self.assertEqual(captured["method"], "PUT")
        self.assertEqual(captured["body"], content)
        headers = captured["headers"]
        self.assertNotIn("authorization", headers)
        self.assertNotIn("cookie", headers)
        self.assertEqual(headers["if-none-match"], "*")

    def test_transfer_redirect_and_bad_headers_fail_without_leaking_signed_url(self) -> None:
        signed_url = "https://objects.example.com/once/file?signature=secret-canary"

        def redirect(request, **_kwargs):
            raise urllib.error.HTTPError(
                request.full_url,
                307,
                "redirect",
                {"Location": "https://attacker.example.com/collect"},
                io.BytesIO(b"private body"),
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.html"
            path.write_bytes(b"ok")
            with open_local_upload(str(path)) as source:
                transfer = {
                    "method": "PUT",
                    "url": signed_url,
                    "headers": {
                        "Content-Length": "2",
                        "Content-Type": "text/html",
                    },
                }
                with self.assertRaises(CliError) as caught:
                    put_upload_transfer(transfer, source, opener=redirect)
                self.assertEqual(caught.exception.code, "LAE_UPLOAD_TRANSFER_REDIRECTED")
                self.assertNotIn("secret-canary", str(caught.exception))

                bad = dict(transfer)
                bad["headers"] = {
                    "Content-Length": "2",
                    "Content-Type": "text/html",
                    "Authorization": "private-canary",
                }
                with self.assertRaises(CliError) as caught:
                    put_upload_transfer(bad, source, opener=redirect)
                self.assertEqual(caught.exception.code, "LAE_API_PROTOCOL_ERROR")
                self.assertNotIn("private-canary", str(caught.exception))

    def test_upload_create_puts_once_and_redacts_transfer(self) -> None:
        signed_url = "https://objects.example.com/once/file?signature=secret-canary"

        class FakeClient:
            calls: list[tuple[object, ...]] = []

            def post(self, path, body=None, *, idempotency_key=None):
                self.calls.append((path, body, idempotency_key))
                return {
                    "upload": upload_body(
                        "upl_test",
                        filename=body["filename"],
                        media_type=body["mediaType"],
                        size_bytes=body["sizeBytes"],
                        digest=body["sha256"],
                        status="quarantine",
                    ),
                    "operation": {"id": "op_upload", "status": "queued"},
                    "uploadUrlIssued": True,
                    "transfer": {
                        "method": "PUT",
                        "url": signed_url,
                        "headers": {
                            "Content-Length": str(body["sizeBytes"]),
                            "Content-Type": body["mediaType"],
                        },
                    },
                }

        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "site.zip"
            path.write_bytes(b"PK-static-fixture")
            stdout = io.StringIO()
            with (
                patch("lae_cli.__main__._client", return_value=client),
                patch("lae_cli.__main__.put_upload_transfer") as transfer,
                redirect_stdout(stdout),
            ):
                code = main(
                    [
                        "uploads",
                        "create",
                        "--app",
                        "app_test",
                        "--file",
                        str(path),
                        "--idempotency-key",
                        "upload-create-test",
                        "--format",
                        "json",
                    ]
                )
        self.assertEqual(code, 0)
        transfer.assert_called_once()
        self.assertEqual(client.calls[0][0], "/uploads")
        self.assertEqual(client.calls[0][2], "upload-create-test")
        rendered = stdout.getvalue()
        self.assertNotIn(signed_url, rendered)
        self.assertNotIn("secret-canary", rendered)
        self.assertNotIn('"transfer"', rendered)
        self.assertTrue(json.loads(rendered)["transferCompleted"])

    def test_upload_show_complete_delete_contracts_require_explicit_keys(self) -> None:
        digest = "sha256:" + hashlib.sha256(b"ok").hexdigest()

        class FakeClient:
            calls: list[tuple[object, ...]] = []

            def get(self, path, *, query=None):
                self.calls.append(("GET", path, query))
                return {
                    "upload": upload_body(
                        "upl_test",
                        filename="index.html",
                        media_type="text/html",
                        size_bytes=2,
                        digest=digest,
                        status="ready",
                    ),
                    "operation": {"id": "op_upload", "status": "succeeded"},
                }

            def post(self, path, body=None, *, idempotency_key=None):
                self.calls.append(("POST", path, body, idempotency_key))
                return {
                    "upload": upload_body(
                        "upl_test",
                        filename="index.html",
                        media_type="text/html",
                        size_bytes=2,
                        digest=digest,
                        status="scanning",
                    ),
                    "operation": {"id": "op_upload", "status": "running"},
                }

            def delete(self, path, *, idempotency_key):
                self.calls.append(("DELETE", path, idempotency_key))
                return {
                    "upload": upload_body(
                        "upl_test",
                        filename="index.html",
                        media_type="text/html",
                        size_bytes=2,
                        digest=digest,
                        status="deleted",
                    ),
                    "operation": {"id": "op_upload", "status": "succeeded"},
                }

        client = FakeClient()
        for arguments in (
            ["uploads", "show", "upl_test", "--format", "json"],
            [
                "uploads",
                "complete",
                "upl_test",
                "--idempotency-key",
                "upload-complete-test",
                "--format",
                "json",
            ],
            [
                "uploads",
                "delete",
                "upl_test",
                "--idempotency-key",
                "upload-delete-test",
                "--format",
                "json",
            ],
        ):
            stdout = io.StringIO()
            with patch("lae_cli.__main__._client", return_value=client), redirect_stdout(
                stdout
            ):
                self.assertEqual(main(arguments), 0)
            self.assertNotIn("signature", stdout.getvalue().lower())
        self.assertEqual(
            client.calls,
            [
                ("GET", "/uploads/upl_test", None),
                (
                    "POST",
                    "/uploads/upl_test/complete",
                    {},
                    "upload-complete-test",
                ),
                ("DELETE", "/uploads/upl_test", "upload-delete-test"),
            ],
        )

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(
                main(["uploads", "complete", "upl_test", "--format", "json"]),
                2,
            )
        self.assertEqual(
            json.loads(stderr.getvalue())["error"]["code"],
            "LAE_CLI_ARGUMENT_INVALID",
        )

    def test_inspect_file_completes_scans_and_creates_upload_analysis(self) -> None:
        class FakeClient:
            calls: list[tuple[object, ...]] = []
            upload_facts: dict[str, object]
            upload_gets = 0

            def post(self, path, body=None, *, idempotency_key=None):
                self.calls.append(("POST", path, body, idempotency_key))
                if path == "/uploads":
                    self.upload_facts = upload_body(
                        "upl_test",
                        filename=body["filename"],
                        media_type=body["mediaType"],
                        size_bytes=body["sizeBytes"],
                        digest=body["sha256"],
                        status="quarantine",
                    )
                    return {
                        "upload": self.upload_facts,
                        "operation": {"id": "op_upload", "status": "queued"},
                        "uploadUrlIssued": True,
                        "transfer": {
                            "method": "PUT",
                            "url": "https://objects.example.com/once/private-signature",
                            "headers": {
                                "Content-Length": str(body["sizeBytes"]),
                                "Content-Type": body["mediaType"],
                            },
                        },
                    }
                if path.endswith("/complete"):
                    return {
                        "upload": {**self.upload_facts, "status": "scanning"},
                        "operation": {"id": "op_upload", "status": "running"},
                    }
                return {
                    "analysis": {"id": "ana_test", "status": "queued"},
                    "operation": {"id": "op_analysis", "status": "queued"},
                }

            def get(self, path, *, query=None):
                self.calls.append(("GET", path, query))
                self.upload_gets += 1
                status = "scanning" if self.upload_gets == 1 else "ready"
                return {
                    "upload": {**self.upload_facts, "status": status},
                    "operation": {
                        "id": "op_upload",
                        "status": "running" if status == "scanning" else "succeeded",
                    },
                }

        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.html"
            path.write_bytes(b"<h1>LAE</h1>")
            stdout = io.StringIO()
            with (
                patch("lae_cli.__main__._client", return_value=client),
                patch("lae_cli.__main__.put_upload_transfer"),
                redirect_stdout(stdout),
            ):
                code = main(
                    [
                        "inspect-file",
                        "--app",
                        "app_test",
                        "--file",
                        str(path),
                        "--idempotency-prefix",
                        "inspect-file-test",
                        "--poll",
                        "0",
                        "--no-wait",
                        "--format",
                        "json",
                    ]
                )
        self.assertEqual(code, 0)
        mutations = [call for call in client.calls if call[0] == "POST"]
        self.assertEqual(mutations[0][3], "inspect-file-test-upload-create")
        self.assertEqual(mutations[1][3], "inspect-file-test-upload-complete")
        self.assertEqual(mutations[2][1], "/analyses")
        self.assertEqual(mutations[2][3], "inspect-file-test-analysis-create")
        self.assertEqual(
            mutations[2][2]["source"], {"type": "upload", "uploadId": "upl_test"}
        )
        rendered = stdout.getvalue()
        self.assertNotIn("private-signature", rendered)
        self.assertEqual(json.loads(rendered)["analysis"]["id"], "ana_test")

    def test_upload_failure_and_timeout_expose_only_safe_resume_facts(self) -> None:
        class FailedClient:
            def get(self, path, *, query=None):
                return {
                    "upload": {
                        **upload_body(
                            "upl_test",
                            filename="index.html",
                            media_type="text/html",
                            size_bytes=2,
                            digest="sha256:" + hashlib.sha256(b"ok").hexdigest(),
                            status="failed",
                            failure_code="LAE_UPLOAD_ARCHIVE_UNSAFE",
                        ),
                        "secret": "must-not-escape",
                    },
                    "operation": {"id": "op_upload", "status": "failed"},
                }

        with self.assertRaises(CliError) as caught:
            _wait_upload_ready(
                FailedClient(),  # type: ignore[arg-type]
                "upl_test",
                timeout_seconds=1,
                poll_seconds=0,
            )
        error = caught.exception
        self.assertEqual(error.exit_code, 7)
        self.assertEqual(error.details["uploadId"], "upl_test")
        self.assertEqual(error.details["failureCode"], "LAE_UPLOAD_ARCHIVE_UNSAFE")
        self.assertNotIn("must-not-escape", str(error.to_dict()))


if __name__ == "__main__":
    unittest.main()
