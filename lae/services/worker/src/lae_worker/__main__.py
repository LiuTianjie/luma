from __future__ import annotations

import asyncio
import os
import sys
from typing import Sequence

from lae_core import component_payload, emit_json, run_component

from .analyze import OrchestrationSchemaUnavailable
from .wiring import WorkerLaneFailure, build_worker_from_env


async def _run_real_once() -> int:
    runtime = None
    try:
        runtime = build_worker_from_env()
        result = await runtime.worker.run_once()
        emit_json(
            component_payload(
                "lae-worker",
                event=(
                    "worker.operation-finished"
                    if result.operation_results
                    else "worker.upload-processed"
                    if result.upload_scanned or result.upload_cleaned
                    else "worker.idle"
                ),
                uploadScanned=result.upload_scanned,
                uploadCleaned=result.upload_cleaned,
                operations=[
                    {"id": operation.id, "status": operation.status}
                    for operation in result.operation_results
                ],
            )
        )
        return 0
    except OrchestrationSchemaUnavailable as exc:
        emit_json(
            component_payload(
                "lae-worker",
                status="blocked",
                event="worker.schema-unavailable",
                code=exc.code,
            )
        )
        return 2
    except WorkerLaneFailure:
        emit_json(
            component_payload(
                "lae-worker",
                status="blocked",
                event="worker.lane-failed",
                code="LAE_WORKER_LANE_FAILED",
            )
        )
        return 2
    finally:
        if runtime is not None:
            await runtime.close()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Preserve the scaffold's zero-config smoke behavior. Once a database is
    # configured, switch to the real PostgreSQL queue/checkpoint wiring.
    if "--once" in arguments and os.environ.get("LAE_DATABASE_URL"):
        return asyncio.run(_run_real_once())
    return run_component("lae-worker", argv, once_event="worker.idle")


if __name__ == "__main__":
    raise SystemExit(main())
