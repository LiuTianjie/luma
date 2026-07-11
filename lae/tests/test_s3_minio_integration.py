from __future__ import annotations

import hashlib
import os
import secrets
import unittest
import urllib.parse

from lae_store import S3PrivateObjectConfig, S3PrivateObjectStore


@unittest.skipUnless(
    os.environ.get("LAE_TEST_MINIO_ENDPOINT"),
    "LAE_TEST_MINIO_ENDPOINT is not configured",
)
class MinioIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_put_head_and_get_stream(self) -> None:
        endpoint = os.environ["LAE_TEST_MINIO_ENDPOINT"]
        hostname = urllib.parse.urlsplit(endpoint).hostname
        if hostname is None:
            self.fail("LAE_TEST_MINIO_ENDPOINT has no hostname")
        store = S3PrivateObjectStore(
            S3PrivateObjectConfig(
                endpoint=endpoint,
                bucket=os.environ["LAE_TEST_MINIO_BUCKET"],
                region=os.environ.get("LAE_TEST_MINIO_REGION", "us-east-1"),
                access_key=os.environ["LAE_TEST_MINIO_ACCESS_KEY"],
                secret_key=os.environ["LAE_TEST_MINIO_SECRET_KEY"],
                allowed_hosts=(hostname,),
                path_style=True,
                production=endpoint.startswith("https://"),
            )
        )
        body = b'{"minio":"verified"}'
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        key = (
            "tenants/minio-integration/analysis-artifacts/evidence/sha256/"
            f"{digest.removeprefix('sha256:')}-{secrets.token_hex(4)}.json"
        )

        async def chunks():
            yield body[:7]
            yield body[7:]

        stored = await store.put_verified(
            key=key,
            media_type="application/json",
            size_bytes=len(body),
            digest=digest,
            chunks=chunks(),
        )
        self.assertEqual(await store.head(key), stored)
        download = await store.get_stream(key, max_bytes=len(body))
        self.assertEqual(
            b"".join([chunk async for chunk in download.chunks]), body
        )


if __name__ == "__main__":
    unittest.main()
