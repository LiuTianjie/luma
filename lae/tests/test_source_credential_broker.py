from __future__ import annotations

import sys
import unittest
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/python/lae-store/src",
    "services/worker/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_store import CredentialLeaseRejected, new_id  # noqa: E402
from lae_worker import UnavailableConnectionCredentialBroker  # noqa: E402


class UnavailableSourceCredentialBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_broker_fails_closed_without_echoing_claim_data(self) -> None:
        lease_id = new_id("lease")
        repository = "https://git.example.com/acme/private.git"
        with self.assertRaises(CredentialLeaseRejected) as caught:
            await UnavailableConnectionCredentialBroker().claim(
                lease_id,
                consumer_id="lae-builder",
                repository=repository,
            )
        message = str(caught.exception)
        self.assertNotIn(lease_id, message)
        self.assertNotIn(repository, message)
        self.assertNotIn("lae-builder", message)


if __name__ == "__main__":
    unittest.main()
