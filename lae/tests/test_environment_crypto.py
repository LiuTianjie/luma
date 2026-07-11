from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-store" / "src"))

from lae_store import (  # noqa: E402
    EnvironmentCryptoError,
    EnvironmentKeyRing,
    EnvironmentPlaintext,
    new_id,
)


class EnvironmentCryptoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tenant_id = new_id("ten")
        self.application_id = new_id("app")
        self.v1 = b"e" * 32
        self.v2 = b"n" * 32
        self.checksum_key = b"h" * 32
        self.ring = EnvironmentKeyRing(
            current_version=1,
            keys={1: self.v1},
            checksum_key=self.checksum_key,
        )

    def test_round_trip_random_nonce_and_secret_safe_repr(self) -> None:
        plaintext = "postgres://user:password@example.test/app"
        ephemeral = EnvironmentPlaintext("web", "DATABASE_URL", plaintext)
        self.assertNotIn(plaintext, repr(ephemeral))

        first = self.ring.encrypt(
            plaintext,
            tenant_id=self.tenant_id,
            application_id=self.application_id,
            service_scope="web",
            name="DATABASE_URL",
        )
        second = self.ring.encrypt(
            plaintext,
            tenant_id=self.tenant_id,
            application_id=self.application_id,
            service_scope="web",
            name="DATABASE_URL",
        )
        self.assertNotEqual(first.envelope, second.envelope)
        self.assertEqual(first.checksum, second.checksum)
        self.assertNotIn(plaintext.encode(), first.envelope)
        self.assertNotIn(plaintext, repr(first))
        self.assertEqual(
            self.ring.decrypt(
                first,
                tenant_id=self.tenant_id,
                application_id=self.application_id,
                service_scope="web",
                name="DATABASE_URL",
            ),
            plaintext,
        )

    def test_aad_checksum_and_unknown_key_fail_closed(self) -> None:
        encrypted = self.ring.encrypt(
            "secret-value",
            tenant_id=self.tenant_id,
            application_id=self.application_id,
            service_scope="*",
            name="API_KEY",
        )
        contexts = (
            (new_id("ten"), self.application_id, "*", "API_KEY"),
            (self.tenant_id, new_id("app"), "*", "API_KEY"),
            (self.tenant_id, self.application_id, "worker", "API_KEY"),
            (self.tenant_id, self.application_id, "*", "OTHER_KEY"),
        )
        for tenant_id, application_id, scope, name in contexts:
            with self.assertRaises(EnvironmentCryptoError):
                self.ring.decrypt(
                    encrypted,
                    tenant_id=tenant_id,
                    application_id=application_id,
                    service_scope=scope,
                    name=name,
                )

        with self.assertRaises(EnvironmentCryptoError):
            self.ring.decrypt(
                replace(encrypted, checksum=b"x" * 32),
                tenant_id=self.tenant_id,
                application_id=self.application_id,
                service_scope="*",
                name="API_KEY",
            )
        with self.assertRaises(EnvironmentCryptoError):
            EnvironmentKeyRing(
                current_version=2,
                keys={2: self.v2},
                checksum_key=self.checksum_key,
            ).decrypt(
                encrypted,
                tenant_id=self.tenant_id,
                application_id=self.application_id,
                service_scope="*",
                name="API_KEY",
            )

    def test_key_rotation_reads_old_and_writes_current_version(self) -> None:
        old = self.ring.encrypt(
            "old-value",
            tenant_id=self.tenant_id,
            application_id=self.application_id,
            service_scope="*",
            name="VALUE",
        )
        rotated = EnvironmentKeyRing(
            current_version=2,
            keys={1: self.v1, 2: self.v2},
            checksum_key=self.checksum_key,
        )
        new = rotated.encrypt(
            "new-value",
            tenant_id=self.tenant_id,
            application_id=self.application_id,
            service_scope="*",
            name="VALUE",
        )
        self.assertEqual(old.key_version, 1)
        self.assertEqual(new.key_version, 2)
        self.assertEqual(
            rotated.decrypt(
                old,
                tenant_id=self.tenant_id,
                application_id=self.application_id,
                service_scope="*",
                name="VALUE",
            ),
            "old-value",
        )
        self.assertEqual(
            rotated.decrypt(
                new,
                tenant_id=self.tenant_id,
                application_id=self.application_id,
                service_scope="*",
                name="VALUE",
            ),
            "new-value",
        )

    def test_key_material_validation_is_strict(self) -> None:
        for kwargs in (
            {"current_version": 1, "keys": {1: b"short"}, "checksum_key": b"h" * 32},
            {"current_version": 2, "keys": {1: self.v1}, "checksum_key": b"h" * 32},
            {"current_version": 1, "keys": {1: self.v1}, "checksum_key": b"short"},
        ):
            with self.assertRaises(ValueError):
                EnvironmentKeyRing(**kwargs)


if __name__ == "__main__":
    unittest.main()
