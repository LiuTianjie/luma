from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-store" / "src"))

from lae_store import (  # noqa: E402
    EncryptedSourceConnectionSecret,
    SourceConnectionCryptoError,
    SourceConnectionKeyRing,
    SourceConnectionPlaintext,
    canonical_source_base_url,
    new_id,
)


class SourceConnectionCryptoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tenant_id = new_id("ten")
        self.connection_id = new_id("conn")
        self.v1_aead = b"a" * 32
        self.v1_hmac = b"h" * 32
        self.ring = SourceConnectionKeyRing(
            current_version=1,
            encryption_keys={1: self.v1_aead},
            hmac_keys={1: self.v1_hmac},
        )
        self.context = {
            "tenant_id": self.tenant_id,
            "connection_id": self.connection_id,
            "provider": "gitea",
            "allowed_host": "git.example.com",
            "username": "deploy",
            "credential_version": 1,
        }

    def test_round_trip_uses_random_nonce_and_secret_safe_repr(self) -> None:
        secret = "pat-secret-canary-42"
        plaintext = SourceConnectionPlaintext(secret)
        self.assertNotIn(secret, repr(plaintext))
        first = self.ring.encrypt(plaintext, **self.context)
        second = self.ring.encrypt(plaintext, **self.context)
        self.assertNotEqual(first.nonce, second.nonce)
        self.assertNotEqual(first.ciphertext, second.ciphertext)
        self.assertEqual(first.checksum, second.checksum)
        self.assertNotIn(secret.encode(), first.ciphertext)
        self.assertNotIn(secret, repr(first))
        recovered = self.ring.decrypt(first, **self.context)
        self.assertEqual(recovered.secret, secret)
        self.assertNotIn(secret, repr(recovered))

    def test_aad_checksum_and_unknown_key_fail_closed(self) -> None:
        encrypted = self.ring.encrypt(
            SourceConnectionPlaintext("credential-canary"), **self.context
        )
        contexts = (
            {**self.context, "tenant_id": new_id("ten")},
            {**self.context, "connection_id": new_id("conn")},
            {**self.context, "provider": "generic"},
            {**self.context, "allowed_host": "other.example.com"},
            {**self.context, "username": "other"},
            {**self.context, "credential_version": 2},
        )
        for context in contexts:
            with self.assertRaises(SourceConnectionCryptoError):
                self.ring.decrypt(encrypted, **context)
        with self.assertRaises(SourceConnectionCryptoError):
            self.ring.decrypt(replace(encrypted, checksum=b"x" * 32), **self.context)
        with self.assertRaises(SourceConnectionCryptoError):
            SourceConnectionKeyRing(
                current_version=2,
                encryption_keys={2: b"b" * 32},
                hmac_keys={2: b"i" * 32},
            ).decrypt(encrypted, **self.context)

    def test_key_rotation_reads_old_and_writes_current(self) -> None:
        old = self.ring.encrypt(SourceConnectionPlaintext("old"), **self.context)
        rotated = SourceConnectionKeyRing(
            current_version=2,
            encryption_keys={1: self.v1_aead, 2: b"b" * 32},
            hmac_keys={1: self.v1_hmac, 2: b"i" * 32},
        )
        new_context = {**self.context, "credential_version": 2}
        new = rotated.encrypt(SourceConnectionPlaintext("new"), **new_context)
        self.assertEqual(old.key_version, 1)
        self.assertEqual(new.key_version, 2)
        self.assertEqual(rotated.decrypt(old, **self.context).secret, "old")
        self.assertEqual(rotated.decrypt(new, **new_context).secret, "new")

    def test_lease_binding_is_consumer_connection_and_host_specific(self) -> None:
        fields = {
            "key_version": 1,
            "tenant_id": self.tenant_id,
            "lease_id": new_id("lease"),
            "connection_id": self.connection_id,
            "builder_task_id": new_id("btask"),
            "consumer_id": "lae-builder",
            "allowed_host": "git.example.com",
        }
        digest = self.ring.lease_binding_digest(**fields)
        self.assertEqual(len(digest), 32)
        self.assertTrue(self.ring.verify_lease_binding(digest, **fields))
        for key, value in (
            ("tenant_id", new_id("ten")),
            ("lease_id", new_id("lease")),
            ("connection_id", new_id("conn")),
            ("builder_task_id", new_id("btask")),
            ("consumer_id", "other-builder"),
            ("allowed_host", "other.example.com"),
        ):
            self.assertFalse(
                self.ring.verify_lease_binding(digest, **{**fields, key: value})
            )

    def test_canonical_base_url_keeps_exact_public_https_origin(self) -> None:
        self.assertEqual(
            canonical_source_base_url("https://Git.Example.COM:443/gitea/"),
            "https://git.example.com/gitea",
        )
        self.assertEqual(
            canonical_source_base_url("https://github.com/"),
            "https://github.com",
        )
        for value in (
            "http://git.example.com",
            "https://user:secret@git.example.com",
            "https://git.example.com?token=secret",
            "https://127.0.0.1",
            "https://[::1]",
            "https://localhost",
            "https://git",
            "https://git.internal",
            "https://git.local",
            "https://git.example.com/#fragment",
        ):
            with self.assertRaises(ValueError, msg=value):
                canonical_source_base_url(value)

    def test_secret_and_key_material_validation_is_strict(self) -> None:
        for secret in ("", "line\nbreak", "nul\0byte", "x" * 4097):
            with self.assertRaises(ValueError):
                self.ring.encrypt(SourceConnectionPlaintext(secret), **self.context)
        for kwargs in (
            {
                "current_version": 1,
                "encryption_keys": {1: b"short"},
                "hmac_keys": {1: b"h" * 32},
            },
            {
                "current_version": 1,
                "encryption_keys": {1: b"a" * 32},
                "hmac_keys": {1: b"short"},
            },
            {
                "current_version": 2,
                "encryption_keys": {1: b"a" * 32},
                "hmac_keys": {1: b"h" * 32},
            },
            {
                "current_version": 1,
                "encryption_keys": {1: b"a" * 32},
                "hmac_keys": {2: b"h" * 32},
            },
        ):
            with self.assertRaises(ValueError):
                SourceConnectionKeyRing(**kwargs)

    def test_ciphertext_dataclass_repr_never_contains_bytes(self) -> None:
        encrypted = EncryptedSourceConnectionSecret(
            ciphertext=b"credential-canary",
            nonce=b"n" * 12,
            checksum=b"h" * 32,
            key_version=1,
        )
        self.assertNotIn("credential-canary", repr(encrypted))


if __name__ == "__main__":
    unittest.main()
