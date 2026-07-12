from __future__ import annotations

import json
import tempfile
from pathlib import Path

from lae_agent_core import analyze_source


REQUIRED_RESULT_FIELDS = {
    "schemaVersion",
    "status",
    "decision",
    "externalOperationId",
    "tenantRef",
    "applicationRef",
    "resolvedCommit",
    "sourceSnapshotId",
    "sourceSnapshotDigest",
    "policyVersion",
    "verdict",
    "diagnosticStatus",
    "diagnosticMode",
    "diagnosticCode",
    "knowledgeVersion",
    "blockers",
    "artifacts",
}


def verify() -> None:
    with tempfile.TemporaryDirectory(prefix="lae-agent-contract-") as temporary:
        root = Path(temporary)
        source = root / "source"
        output = root / "output"
        source.mkdir()
        (source / "index.html").write_text(
            "<!doctype html><title>LAE agent contract probe</title>",
            encoding="utf-8",
        )
        metadata = {
            "schemaVersion": "lae.agent-analysis-metadata/v1",
            "builderTaskId": "builder-image-contract-probe",
            "externalOperationId": "operation-image-contract-probe",
            "tenantRef": "tenant-image-contract-probe",
            "applicationRef": "application-image-contract-probe",
            "resolvedCommit": "0" * 40,
            "sourceTreeDigest": "sha256:" + "1" * 64,
            "sourceSnapshotId": "snapshot-image-contract-probe",
            "sourceSnapshotDigest": "sha256:" + "2" * 64,
            "policyVersion": "builder-image-contract-probe",
            "agentImageDigest": "registry.invalid/lae/agent-runner@sha256:" + "3" * 64,
        }
        analyze_source(source, metadata, output)
        result = json.loads((output / "result.json").read_text(encoding="utf-8"))

    missing = sorted(REQUIRED_RESULT_FIELDS - set(result))
    if missing:
        raise SystemExit(
            "agent runner result contract is stale; missing fields: "
            + ", ".join(missing)
        )
    if result.get("schemaVersion") != "lae.agent-analysis-result/v1":
        raise SystemExit("agent runner result schemaVersion is incompatible")
    if result.get("status") != "succeeded" or result.get("verdict") != "deployable":
        raise SystemExit("agent runner contract probe did not produce a deployable result")
    if result.get("diagnosticStatus") not in {"succeeded", "diagnostic_failed"}:
        raise SystemExit("agent runner diagnostic status is incompatible")
    if result.get("diagnosticMode") not in {"ai", "deterministic_fallback"}:
        raise SystemExit("agent runner diagnostic mode is incompatible")
    if not isinstance(result.get("diagnosticCode"), str):
        raise SystemExit("agent runner diagnostic code is missing")
    if not isinstance(result.get("knowledgeVersion"), str):
        raise SystemExit("agent runner knowledge version is missing")
    if not isinstance(result.get("blockers"), list):
        raise SystemExit("agent runner blockers are incompatible")
    if set(result.get("artifacts") or {}) != {
        "evidence",
        "deploymentPlan",
        "buildPlan",
    }:
        raise SystemExit("agent runner artifact contract is incompatible")


if __name__ == "__main__":
    verify()
