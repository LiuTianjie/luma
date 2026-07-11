from __future__ import annotations

import hashlib
import io
import stat
import struct
import sys
import unittest
import zipfile
from datetime import timedelta
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/python/lae-core/src",
    "packages/python/lae-store/src",
    "services/worker/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_store import (  # noqa: E402
    FakeUploadObjectStore,
    S3SigV4UploadStore,
    S3UploadConfig,
    UnconfiguredUploadStore,
    UploadUnavailable,
    UploadVerificationFailed,
)
from lae_worker.static_upload import (  # noqa: E402
    StaticArtifactRejected,
    StaticValidationPolicy,
    validate_static_artifact,
)


def _zip(entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def _valid_entries() -> list[tuple[str, bytes]]:
    return [
        ("index.html", b"<!doctype html><html><body>Hello</body></html>"),
        ("assets/app.css", b"body{color:#111}"),
        ("assets/app.js", b"console.log('ok')"),
    ]


class StaticArtifactValidationTests(unittest.TestCase):
    def policy(self, **changes: int) -> StaticValidationPolicy:
        values = {
            "max_archive_bytes": 4 * 1024 * 1024,
            "max_unpacked_bytes": 8 * 1024 * 1024,
            "max_files": 100,
            "max_path_bytes": 240,
            "max_compression_ratio": 100,
        }
        values.update(changes)
        return StaticValidationPolicy(**values)

    def validate_zip(self, content: bytes, **changes: int):
        return validate_static_artifact(
            io.BytesIO(content),
            filename="site.zip",
            media_type="application/zip",
            policy=self.policy(**changes),
        )

    def assert_rejected(self, content: bytes, code: str, **changes: int) -> None:
        with self.assertRaises(StaticArtifactRejected) as caught:
            self.validate_zip(content, **changes)
        self.assertEqual(caught.exception.code, code)

    def test_valid_html_and_zip_have_deterministic_tree_digest(self) -> None:
        html = b"\xef\xbb\xbf  <!DOCTYPE html><html lang='zh'></html>"
        facts = validate_static_artifact(
            io.BytesIO(html),
            filename="landing.HTML",
            media_type="text/html",
            policy=self.policy(),
        )
        self.assertEqual(facts.file_count, 1)
        first = self.validate_zip(_zip(_valid_entries()))
        second = self.validate_zip(_zip(list(reversed(_valid_entries()))))
        self.assertEqual(first.source_tree_digest, second.source_tree_digest)
        self.assertEqual(first.file_count, 3)

    def test_path_traversal_absolute_nul_long_and_casefold_duplicates_rejected(self) -> None:
        for name in ("../index.html", "/index.html", "C:/index.html", "a\\index.html"):
            self.assert_rejected(
                _zip([(name, b"<!doctype html><html></html>")]),
                "LAE_UPLOAD_ZIP_PATH_INVALID",
            )
        self.assert_rejected(
            _zip(_valid_entries() + [("INDEX.HTML", b"other")]),
            "LAE_UPLOAD_DUPLICATE_PATH",
        )
        self.assert_rejected(
            _zip([("index.html", b"<!doctype html><html></html>"), ("a" * 241, b"x")]),
            "LAE_UPLOAD_ZIP_PATH_INVALID",
        )
        content = bytearray(_zip(_valid_entries()))
        central = content.index(b"PK\x01\x02")
        name_length = int.from_bytes(content[central + 28 : central + 30], "little")
        self.assertGreater(name_length, 2)
        content[central + 47] = 0
        self.assert_rejected(bytes(content), "LAE_UPLOAD_ZIP_PATH_INVALID")

    def test_symlink_special_hardlink_and_executable_entries_rejected(self) -> None:
        symlink = zipfile.ZipInfo("index.html")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.assert_rejected(
            _zip([(symlink, b"target")]), "LAE_UPLOAD_ZIP_SPECIAL_FILE"
        )
        hardlink = zipfile.ZipInfo("index.html")
        hardlink.create_system = 3
        hardlink.external_attr = (stat.S_IFREG | 0o644) << 16
        hardlink.extra = struct.pack("<HH", 0x000D, 0)
        self.assert_rejected(
            _zip([(hardlink, b"<!doctype html><html></html>")]),
            "LAE_UPLOAD_ZIP_LINK_METADATA",
        )
        executable = zipfile.ZipInfo("index.html")
        executable.create_system = 3
        executable.external_attr = (stat.S_IFREG | 0o755) << 16
        self.assert_rejected(
            _zip([(executable, b"<!doctype html><html></html>")]),
            "LAE_UPLOAD_EXECUTABLE_FORBIDDEN",
        )
        self.assert_rejected(
            _zip(_valid_entries() + [("assets/tool.exe", b"MZpayload")]),
            "LAE_UPLOAD_EXECUTABLE_FORBIDDEN",
        )

    def test_nested_archives_missing_index_and_executable_magic_rejected(self) -> None:
        self.assert_rejected(
            _zip(_valid_entries() + [("assets/data.dat", b"PK\x03\x04nested")]),
            "LAE_UPLOAD_NESTED_ARCHIVE",
        )
        self.assert_rejected(
            _zip(_valid_entries() + [("assets/data.zip", b"not even a zip")]),
            "LAE_UPLOAD_NESTED_ARCHIVE",
        )
        self.assert_rejected(
            _zip([("page.html", b"<!doctype html><html></html>")]),
            "LAE_UPLOAD_INDEX_REQUIRED",
        )
        self.assert_rejected(
            _zip(_valid_entries() + [("assets/blob.dat", b"\x7fELFpayload")]),
            "LAE_UPLOAD_EXECUTABLE_FORBIDDEN",
        )

    def test_bomb_file_count_multidisk_and_encryption_guards(self) -> None:
        self.assert_rejected(
            _zip(_valid_entries() + [("assets/bomb.txt", b"A" * 1_000_000)]),
            "LAE_UPLOAD_COMPRESSION_RATIO",
        )
        self.assert_rejected(
            _zip(_valid_entries()), "LAE_UPLOAD_TOO_MANY_FILES", max_files=2
        )
        multidisk = bytearray(_zip(_valid_entries()))
        eocd = multidisk.rfind(b"PK\x05\x06")
        multidisk[eocd + 4 : eocd + 6] = (1).to_bytes(2, "little")
        self.assert_rejected(bytes(multidisk), "LAE_UPLOAD_MULTIDISK_OR_ZIP64")
        encrypted = bytearray(_zip(_valid_entries()))
        local = encrypted.index(b"PK\x03\x04")
        central = encrypted.index(b"PK\x01\x02")
        encrypted[local + 6 : local + 8] = (1).to_bytes(2, "little")
        encrypted[central + 8 : central + 10] = (1).to_bytes(2, "little")
        self.assert_rejected(bytes(encrypted), "LAE_UPLOAD_ZIP_ENCRYPTED")

    def test_html_mime_sniff_encoding_and_nul_are_strict(self) -> None:
        for body in (b"plain text", b"<html>\x00</html>", b"\xff<html></html>"):
            with self.assertRaises(StaticArtifactRejected):
                validate_static_artifact(
                    io.BytesIO(body),
                    filename="index.html",
                    media_type="text/html",
                    policy=self.policy(),
                )
        with self.assertRaises(StaticArtifactRejected) as caught:
            validate_static_artifact(
                io.BytesIO(b"<!doctype html><html></html>"),
                filename="index.html",
                media_type="application/octet-stream",
                policy=self.policy(),
            )
        self.assertEqual(caught.exception.code, "LAE_UPLOAD_TYPE_INVALID")


class UploadObjectStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_grant_is_single_use_size_and_headers_bound(self) -> None:
        store = FakeUploadObjectStore()
        key = "tenants/ten_x/apps/app_x/quarantine/upl_x/random.html"
        content = b"<!doctype html><html></html>"
        grant = await store.issue_single_use_put(
            object_key=key,
            size_bytes=len(content),
            media_type="text/html",
            expires_in=timedelta(minutes=5),
        )
        store.put_from_grant(grant.url, content, headers=grant.headers)
        with self.assertRaises(UploadVerificationFailed):
            store.put_from_grant(grant.url, content, headers=grant.headers)
        sink = io.BytesIO()
        metadata = await store.copy_to(key, sink, max_bytes=len(content))
        self.assertEqual(sink.getvalue(), content)
        self.assertEqual(metadata.size_bytes, len(content))
        self.assertNotIn(grant.url, repr(grant))

    async def test_s3_presign_binds_atomic_create_only_headers_and_redacts_secret(self) -> None:
        store = S3SigV4UploadStore(
            S3UploadConfig(
                endpoint="https://objects.example.test",
                bucket="lae-uploads",
                region="us-east-1",
                access_key="AKIDEXAMPLE",
                secret_key="secret-value-never-rendered",
                production=True,
            )
        )
        grant = await store.issue_single_use_put(
            object_key="tenants/ten_x/apps/app_x/quarantine/upl_x/abc.zip",
            size_bytes=123,
            media_type="application/zip",
            expires_in=timedelta(minutes=5),
        )
        self.assertEqual(grant.headers["If-None-Match"], "*")
        self.assertEqual(grant.headers["Content-Length"], "123")
        self.assertIn("X-Amz-Signature=", grant.url)
        self.assertNotIn("secret-value", grant.url)
        self.assertNotIn(grant.url, repr(grant))
        self.assertNotIn("secret-value", repr(store))

        read_grant = await store.issue_bounded_get(
            object_key="tenants/ten_x/apps/app_x/quarantine/upl_x/abc.zip",
            expires_in=timedelta(seconds=90),
        )
        self.assertIn("X-Amz-Expires=90", read_grant.url)
        self.assertIn("X-Amz-SignedHeaders=host", read_grant.url)
        self.assertNotIn("secret-value", read_grant.url)
        self.assertNotIn(read_grant.url, repr(read_grant))

    async def test_fake_internal_read_grant_is_consumed_once(self) -> None:
        store = FakeUploadObjectStore()
        key = "tenants/ten_x/apps/app_x/quarantine/upl_x/read.html"
        content = b"<!doctype html><html></html>"
        store.seed(key, content, "text/html")
        grant = await store.issue_bounded_get(
            object_key=key,
            expires_in=timedelta(seconds=60),
        )
        self.assertEqual(store.get_from_grant(grant.url), content)
        with self.assertRaises(UploadVerificationFailed):
            store.get_from_grant(grant.url)

    async def test_production_http_and_unconfigured_adapter_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            S3UploadConfig(
                endpoint="http://minio:9000",
                bucket="uploads",
                region="us-east-1",
                access_key="access",
                secret_key="secret-secret-secret",
                production=True,
            )
        with self.assertRaises(UploadUnavailable):
            UnconfiguredUploadStore().ensure_available()


if __name__ == "__main__":
    unittest.main()
