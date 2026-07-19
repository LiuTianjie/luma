from __future__ import annotations

import asyncio
import hashlib
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lae_store import (
    PrivateObjectIntegrityError,
    PrivateObjectStoreUnavailable,
    S3PrivateObjectConfig,
    S3PrivateObjectStore,
)


class _S3Handler(BaseHTTPRequestHandler):
    objects: dict[str, tuple[bytes, str, str]] = {}

    def log_message(self, _format: str, *_args: object) -> None:
        return None

    def do_HEAD(self) -> None:
        if self.path.endswith("redirect.json"):
            self.send_response(307)
            self.send_header("Location", "http://redirect.invalid/object")
            self.end_headers()
            return
        value = self.objects.get(self.path)
        if value is None:
            self.send_response(404)
            self.end_headers()
            return
        body, media_type, digest = value
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", media_type)
        self.send_header("X-Amz-Meta-LAE-SHA256", digest)
        self.end_headers()

    def do_GET(self) -> None:
        value = self.objects.get(self.path)
        if value is None:
            self.send_response(404)
            self.end_headers()
            return
        body, media_type, digest = value
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", media_type)
        self.send_header("X-Amz-Meta-LAE-SHA256", digest)
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self) -> None:
        if self.headers.get("If-None-Match") != "*":
            self.send_response(400)
            self.end_headers()
            return
        if self.path in self.objects:
            self.send_response(412)
            self.end_headers()
            return
        size = int(self.headers["Content-Length"])
        body = self.rfile.read(size)
        self.objects[self.path] = (
            body,
            self.headers["Content-Type"],
            self.headers["X-Amz-Meta-LAE-SHA256"],
        )
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class S3PrivateObjectStoreTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _S3Handler.objects = {}
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _S3Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        _S3Handler.objects.clear()
        self.secret = "secret-canary-0123456789"
        self.config = S3PrivateObjectConfig(
            endpoint=f"http://127.0.0.1:{self.server.server_port}",
            bucket="lae-artifacts",
            region="us-east-1",
            access_key="access-canary",
            secret_key=self.secret,
            allowed_hosts=("127.0.0.1",),
            production=False,
            path_style=True,
            timeout_seconds=2,
        )
        self.store = S3PrivateObjectStore(self.config)
        self.key = "tenants/tenant-test/analysis-artifacts/evidence/sha256/" + (
            "a" * 64
        ) + ".json"
        self.body = b'{"schemaVersion":"test/v1"}'
        self.digest = "sha256:" + hashlib.sha256(self.body).hexdigest()

    async def _chunks(self, body: bytes | None = None):
        value = self.body if body is None else body
        for offset in range(0, len(value), 5):
            await asyncio.sleep(0)
            yield value[offset : offset + 5]

    async def test_verified_put_head_and_stream_are_idempotent(self) -> None:
        stored = await self.store.put_verified(
            key=self.key,
            media_type="application/json",
            size_bytes=len(self.body),
            digest=self.digest,
            chunks=self._chunks(),
        )
        self.assertEqual(stored, await self.store.head(self.key))
        download = await self.store.get_stream(self.key, max_bytes=len(self.body))
        received = b"".join([chunk async for chunk in download.chunks])
        self.assertEqual(received, self.body)

        second = await self.store.put_verified(
            key=self.key,
            media_type="application/json",
            size_bytes=len(self.body),
            digest=self.digest,
            chunks=self._chunks(),
        )
        self.assertEqual(second, stored)
        self.assertEqual(len(_S3Handler.objects), 1)

    async def test_invalid_or_canceled_stream_never_publishes(self) -> None:
        with self.assertRaises(PrivateObjectIntegrityError):
            await self.store.put_verified(
                key=self.key,
                media_type="application/json",
                size_bytes=len(self.body),
                digest=self.digest,
                chunks=self._chunks(self.body + b"x"),
            )
        self.assertFalse(_S3Handler.objects)

        gate = asyncio.Event()

        async def blocked():
            yield self.body[:4]
            await gate.wait()
            yield self.body[4:]

        task = asyncio.create_task(
            self.store.put_verified(
                key=self.key,
                media_type="application/json",
                size_bytes=len(self.body),
                digest=self.digest,
                chunks=blocked(),
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(_S3Handler.objects)

    async def test_redirect_and_existing_mismatch_fail_closed(self) -> None:
        redirect_key = "private/redirect.json"
        with self.assertRaises(PrivateObjectStoreUnavailable):
            await self.store.head(redirect_key)

        path = "/lae-artifacts/" + self.key
        _S3Handler.objects[path] = (
            b"different",
            "application/json",
            "sha256:" + "b" * 64,
        )
        with self.assertRaises(PrivateObjectIntegrityError):
            await self.store.put_verified(
                key=self.key,
                media_type="application/json",
                size_bytes=len(self.body),
                digest=self.digest,
                chunks=self._chunks(),
            )

        _S3Handler.objects[path] = (
            b"x" * len(self.body),
            "application/json",
            self.digest,
        )
        with self.assertRaisesRegex(
            PrivateObjectIntegrityError, "content does not match"
        ):
            await self.store.put_verified(
                key=self.key,
                media_type="application/json",
                size_bytes=len(self.body),
                digest=self.digest,
                chunks=self._chunks(),
            )

    async def test_config_and_errors_do_not_disclose_credentials(self) -> None:
        rendered = repr(self.config) + repr(self.store)
        self.assertNotIn(self.secret, rendered)
        self.assertNotIn("access-canary", rendered)
        with self.assertRaises(PrivateObjectStoreUnavailable) as caught:
            await self.store.head("private/redirect.json")
        self.assertNotIn(self.secret, str(caught.exception))
        self.assertNotIn("access-canary", str(caught.exception))

    def test_production_requires_tls_and_exact_host_allowlist(self) -> None:
        with self.assertRaisesRegex(ValueError, "endpoint is invalid"):
            S3PrivateObjectConfig(
                endpoint="http://artifact-store:9000",
                bucket="lae-artifacts",
                region="us-east-1",
                access_key="access",
                secret_key=self.secret,
                allowed_hosts=("artifact-store",),
                production=True,
            )
        with self.assertRaisesRegex(ValueError, "endpoint is invalid"):
            S3PrivateObjectConfig(
                endpoint="https://artifact-store:9000",
                bucket="lae-artifacts",
                region="us-east-1",
                access_key="access",
                secret_key=self.secret,
                allowed_hosts=("other-store",),
                production=True,
            )


if __name__ == "__main__":
    unittest.main()
