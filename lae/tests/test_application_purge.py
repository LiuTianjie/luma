from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages/python/lae-store/src"))

from lae_store.application_purge import ApplicationHistoryPurgeStore  # noqa: E402


class ApplicationHistoryPurgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_skips_when_application_not_soft_deleted(self) -> None:
        application = MagicMock()
        application.deleted_at = None
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=application)
        session.scalars = AsyncMock()
        session.begin = MagicMock()
        session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
        session.begin.return_value.__aexit__ = AsyncMock(return_value=None)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=None)
        factory = MagicMock(return_value=session_cm)

        store = ApplicationHistoryPurgeStore(factory)
        counts = await store.purge_deleted_application_history(
            tenant_id="ten_test", application_id="app_test"
        )
        self.assertEqual(counts, {})
        session.scalars.assert_not_called()

    async def test_returns_empty_when_no_operations(self) -> None:
        application = MagicMock()
        application.deleted_at = object()
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=application)

        async def empty_scalars(_stmt):
            result = MagicMock()
            result.__iter__ = lambda self: iter(())
            return result

        # session.scalars returns awaitable of iterable of ids
        session.scalars = AsyncMock(
            side_effect=[
                MagicMock(__iter__=lambda self: iter(())),  # app_ops
                MagicMock(__iter__=lambda self: iter(())),  # analysis_ops
                MagicMock(__iter__=lambda self: iter(())),  # source_ids
            ]
        )
        session.begin = MagicMock()
        session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
        session.begin.return_value.__aexit__ = AsyncMock(return_value=None)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=None)
        factory = MagicMock(return_value=session_cm)

        store = ApplicationHistoryPurgeStore(factory)
        counts = await store.purge_deleted_application_history(
            tenant_id="ten_test", application_id="app_test"
        )
        self.assertEqual(counts, {})


if __name__ == "__main__":
    unittest.main()
