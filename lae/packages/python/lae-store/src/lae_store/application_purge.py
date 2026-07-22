"""Purge durable audit rows after an application is soft-deleted."""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import Analysis, Application, Operation, SourceRevision


class ApplicationHistoryPurgeStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def purge_deleted_application_history(
        self, *, tenant_id: str, application_id: str
    ) -> dict[str, int]:
        """Delete operations/events tied to a soft-deleted application."""
        counts: dict[str, int] = {}
        async with self._sessions() as session:
            async with session.begin():
                application = await session.scalar(
                    select(Application).where(
                        Application.tenant_id == tenant_id,
                        Application.id == application_id,
                    )
                )
                if application is None or application.deleted_at is None:
                    return counts

                app_ops = list(
                    await session.scalars(
                        select(Operation.id).where(
                            Operation.tenant_id == tenant_id,
                            Operation.target_type == "application",
                            Operation.target_id == application_id,
                        )
                    )
                )
                analysis_ops = list(
                    await session.scalars(
                        select(Analysis.operation_id).where(
                            Analysis.tenant_id == tenant_id,
                            Analysis.application_id == application_id,
                        )
                    )
                )
                source_ids = list(
                    await session.scalars(
                        select(SourceRevision.id).where(
                            SourceRevision.tenant_id == tenant_id,
                            SourceRevision.application_id == application_id,
                        )
                    )
                )
                source_ops: list[str] = []
                if source_ids:
                    source_ops = list(
                        await session.scalars(
                            select(Operation.id).where(
                                Operation.tenant_id == tenant_id,
                                Operation.target_type == "source-revision",
                                Operation.target_id.in_(source_ids),
                            )
                        )
                    )
                all_ops = sorted({*app_ops, *analysis_ops, *source_ops})
                if not all_ops:
                    return counts

                for table in (
                    "idempotency_records",
                    "operation_events",
                    "analysis_artifacts",
                    "analyses",
                    "builder_tasks",
                    "source_credential_leases",
                    "deployments",
                    "uploads",
                    "deployment_quota_reservations",
                    "deployment_checkpoints",
                    "deployment_build_outputs",
                    "application_lifecycle_requests",
                ):
                    exists = await session.scalar(
                        text(
                            "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema = 'public' AND table_name = :t"
                        ),
                        {"t": table},
                    )
                    if not exists:
                        continue
                    # analysis_artifacts has no operation_id — skip until analyses cleared
                    if table == "analysis_artifacts":
                        result = await session.execute(
                            text(
                                "DELETE FROM analysis_artifacts WHERE tenant_id = :tenant "
                                "AND analysis_id IN ("
                                "SELECT id FROM analyses WHERE tenant_id = :tenant "
                                "AND operation_id = ANY(:ops))"
                            ),
                            {"tenant": tenant_id, "ops": all_ops},
                        )
                    else:
                        result = await session.execute(
                            text(
                                f"DELETE FROM {table} WHERE tenant_id = :tenant "
                                "AND operation_id = ANY(:ops)"
                            ),
                            {"tenant": tenant_id, "ops": all_ops},
                        )
                    counts[table] = int(result.rowcount or 0)

                if source_ids:
                    result = await session.execute(
                        text(
                            "DELETE FROM source_revisions WHERE tenant_id = :tenant "
                            "AND id = ANY(:ids)"
                        ),
                        {"tenant": tenant_id, "ids": source_ids},
                    )
                    counts["source_revisions"] = int(result.rowcount or 0)

                result = await session.execute(
                    text(
                        "DELETE FROM operations WHERE tenant_id = :tenant "
                        "AND id = ANY(:ops)"
                    ),
                    {"tenant": tenant_id, "ops": all_ops},
                )
                counts["operations"] = int(result.rowcount or 0)
        return counts
