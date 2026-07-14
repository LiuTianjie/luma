from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .models import TemplateHealth


_TEMPLATE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


@dataclass(frozen=True, slots=True)
class TemplatePublication:
    template_id: str
    template_version: str
    published: bool
    consecutive_failures: int
    last_status: str
    last_error_code: str | None = None


class PostgresTemplateHealthStore:
    def __init__(self, sessions: Any, *, failure_threshold: int = 3) -> None:
        if not 1 <= failure_threshold <= 20:
            raise ValueError("template failure threshold is invalid")
        self._sessions = sessions
        self._failure_threshold = failure_threshold

    async def publication(
        self, template_id: str, template_version: str
    ) -> TemplatePublication:
        _validate_identity(template_id, template_version)
        async with self._sessions() as session:
            row = await session.get(TemplateHealth, template_id)
            if row is None or row.template_version != template_version:
                return TemplatePublication(
                    template_id,
                    template_version,
                    True,
                    0,
                    "unverified",
                )
            return _publication(row)

    async def record(
        self,
        *,
        template_id: str,
        template_version: str,
        run_id: str,
        succeeded: bool,
        error_code: str | None = None,
    ) -> TemplatePublication:
        _validate_identity(template_id, template_version)
        if _RUN_ID.fullmatch(run_id) is None:
            raise ValueError("template smoke run id is invalid")
        if succeeded:
            if error_code is not None:
                raise ValueError("successful template smoke cannot have an error")
        elif error_code is None or _ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError("failed template smoke requires a safe error code")

        now = datetime.now(timezone.utc)
        async with self._sessions() as session, session.begin():
            await session.execute(
                insert(TemplateHealth)
                .values(
                    template_id=template_id,
                    template_version=template_version,
                    published=True,
                    consecutive_failures=0,
                    last_status="unverified",
                )
                .on_conflict_do_nothing(index_elements=[TemplateHealth.template_id])
            )
            row = await session.scalar(
                select(TemplateHealth)
                .where(TemplateHealth.template_id == template_id)
                .with_for_update()
            )
            if row is None:
                raise RuntimeError("template health row was not created")
            if row.last_run_id == run_id and row.template_version == template_version:
                return _publication(row)
            if row.template_version != template_version:
                row.template_version = template_version
                row.published = True
                row.consecutive_failures = 0
                row.last_status = "unverified"
                row.last_error_code = None
                row.last_succeeded_at = None
                row.last_failed_at = None
                row.auto_unpublished_at = None

            _apply_result(
                row,
                run_id=run_id,
                succeeded=succeeded,
                error_code=error_code,
                now=now,
                failure_threshold=self._failure_threshold,
            )
            await session.flush()
            return _publication(row)


def _validate_identity(template_id: str, template_version: str) -> None:
    if _TEMPLATE_ID.fullmatch(template_id) is None:
        raise ValueError("template id is invalid")
    if _VERSION.fullmatch(template_version) is None:
        raise ValueError("template version is invalid")


def _apply_result(
    row: TemplateHealth,
    *,
    run_id: str,
    succeeded: bool,
    error_code: str | None,
    now: datetime,
    failure_threshold: int,
) -> None:
    row.last_run_id = run_id
    row.last_checked_at = now
    row.updated_at = now
    if succeeded:
        row.published = True
        row.consecutive_failures = 0
        row.last_status = "succeeded"
        row.last_error_code = None
        row.last_succeeded_at = now
        row.auto_unpublished_at = None
        return
    row.consecutive_failures = int(row.consecutive_failures) + 1
    row.last_status = "failed"
    row.last_error_code = error_code
    row.last_failed_at = now
    if row.consecutive_failures >= failure_threshold:
        row.published = False
        row.auto_unpublished_at = row.auto_unpublished_at or now


def _publication(row: TemplateHealth) -> TemplatePublication:
    return TemplatePublication(
        template_id=row.template_id,
        template_version=row.template_version,
        published=bool(row.published),
        consecutive_failures=int(row.consecutive_failures),
        last_status=row.last_status,
        last_error_code=row.last_error_code,
    )


__all__ = [
    "PostgresTemplateHealthStore",
    "TemplatePublication",
]
