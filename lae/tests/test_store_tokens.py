from __future__ import annotations

import sys
import unittest
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-store" / "src"))

from lae_store.ids import new_id, require_opaque_id  # noqa: E402
from lae_store.models import DeployToken  # noqa: E402
from lae_store.tokens import (  # noqa: E402
    issue_deploy_token,
    keyed_request_hash,
    keyed_secret_hash,
    parse_deploy_token,
    verify_deploy_token,
)


class StoreIdentifierTests(unittest.TestCase):
    def test_ids_are_opaque_prefixed_ulids(self) -> None:
        first = new_id("app", timestamp_ms=1_700_000_000_000)
        second = new_id("app", timestamp_ms=1_700_000_000_001)
        self.assertRegex(first, r"^app_[0-9A-HJKMNP-TV-Z]{26}$")
        self.assertLess(first, second)
        self.assertEqual(require_opaque_id(first, prefix="app"), first)
        with self.assertRaises(ValueError):
            require_opaque_id("app_customer-email@example.com", prefix="app")


class DeployTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = b"k" * 32

    def test_issued_token_has_256_bit_secret_and_only_hash_is_persistable(self) -> None:
        issued = issue_deploy_token(self.key, key_version=3)
        prefix, secret = parse_deploy_token(issued.plaintext)
        self.assertEqual(prefix, issued.prefix)
        self.assertEqual(len(secret), 43)
        self.assertEqual(len(issued.digest), 32)
        self.assertTrue(
            verify_deploy_token(
                issued.plaintext,
                expected_digest=issued.digest,
                key=self.key,
            )
        )
        self.assertFalse(
            verify_deploy_token(
                issued.plaintext[:-1] + ("A" if issued.plaintext[-1] != "A" else "B"),
                expected_digest=issued.digest,
                key=self.key,
            )
        )
        self.assertNotIn(issued.plaintext, repr(issued))

        columns = set(DeployToken.__table__.columns.keys())
        self.assertIn("token_hash", columns)
        self.assertIn("prefix", columns)
        self.assertFalse(columns & {"token", "plaintext", "secret", "token_ciphertext"})

    def test_hashes_are_keyed_and_domain_separated(self) -> None:
        value = "same-secret"
        first = keyed_secret_hash(value, self.key, domain="lae.deploy-token.v1")
        second = keyed_secret_hash(value, b"z" * 32, domain="lae.deploy-token.v1")
        session = keyed_secret_hash(value, self.key, domain="lae.session.v1")
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, session)
        self.assertNotIn(value.encode(), first)

    def test_idempotency_hash_uses_canonical_json_and_hmac(self) -> None:
        first = keyed_request_hash({"b": 2, "a": [1, True]}, self.key)
        second = keyed_request_hash({"a": [1, True], "b": 2}, self.key)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)
        with self.assertRaises(ValueError):
            keyed_request_hash({"invalid": float("nan")}, self.key)


if __name__ == "__main__":
    unittest.main()
