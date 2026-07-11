from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("update-check digest is invalid")
    return value


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    """Closed, secret-free comparison between one deployed baseline and candidate.

    The baseline is deliberately all-or-nothing.  A legacy or pending application
    can have a reusable Git source without a deployed revision; callers must not
    interpret that partial state as evidence that a deployment plan is unchanged.
    """

    baseline_available: bool
    source_changed: bool
    deployment_plan_changed: bool
    changed: bool
    candidate_source_tree_digest: str
    candidate_deployment_plan_digest: str
    baseline_source_tree_digest: str | None = None
    baseline_deployment_plan_digest: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.baseline_available,
            self.source_changed,
            self.deployment_plan_changed,
            self.changed,
        ):
            if not isinstance(value, bool):
                raise ValueError("update-check flags must be booleans")
        _digest(self.candidate_source_tree_digest)
        _digest(self.candidate_deployment_plan_digest)
        if self.baseline_available:
            _digest(self.baseline_source_tree_digest)
            _digest(self.baseline_deployment_plan_digest)
            if self.changed != (
                self.source_changed or self.deployment_plan_changed
            ):
                raise ValueError("update-check changed flag is inconsistent")
        elif (
            self.baseline_source_tree_digest is not None
            or self.baseline_deployment_plan_digest is not None
            or not self.source_changed
            or not self.deployment_plan_changed
            or not self.changed
        ):
            # Missing baselines are conservative: the platform cannot claim
            # either dimension is unchanged.
            raise ValueError("unavailable update-check baseline must be conservative")

    def to_body(self) -> dict[str, Any]:
        baseline: dict[str, str] | None = None
        if self.baseline_available:
            assert self.baseline_source_tree_digest is not None
            assert self.baseline_deployment_plan_digest is not None
            baseline = {
                "sourceTree": self.baseline_source_tree_digest,
                "deploymentPlan": self.baseline_deployment_plan_digest,
            }
        return {
            "baselineAvailable": self.baseline_available,
            "sourceChanged": self.source_changed,
            "deploymentPlanChanged": self.deployment_plan_changed,
            "changed": self.changed,
            "digests": {
                "baseline": baseline,
                "candidate": {
                    "sourceTree": self.candidate_source_tree_digest,
                    "deploymentPlan": self.candidate_deployment_plan_digest,
                },
            },
        }

    @classmethod
    def from_body(cls, value: object) -> "UpdateCheckResult":
        if not isinstance(value, Mapping) or set(value) != {
            "baselineAvailable",
            "sourceChanged",
            "deploymentPlanChanged",
            "changed",
            "digests",
        }:
            raise ValueError("update-check result shape is invalid")
        digests = value["digests"]
        if not isinstance(digests, Mapping) or set(digests) != {
            "baseline",
            "candidate",
        }:
            raise ValueError("update-check digests shape is invalid")
        candidate = digests["candidate"]
        if not isinstance(candidate, Mapping) or set(candidate) != {
            "sourceTree",
            "deploymentPlan",
        }:
            raise ValueError("update-check candidate shape is invalid")
        baseline = digests["baseline"]
        baseline_source: str | None = None
        baseline_plan: str | None = None
        if baseline is not None:
            if not isinstance(baseline, Mapping) or set(baseline) != {
                "sourceTree",
                "deploymentPlan",
            }:
                raise ValueError("update-check baseline shape is invalid")
            baseline_source = _digest(baseline["sourceTree"])
            baseline_plan = _digest(baseline["deploymentPlan"])
        return cls(
            baseline_available=value["baselineAvailable"],
            source_changed=value["sourceChanged"],
            deployment_plan_changed=value["deploymentPlanChanged"],
            changed=value["changed"],
            baseline_source_tree_digest=baseline_source,
            baseline_deployment_plan_digest=baseline_plan,
            candidate_source_tree_digest=_digest(candidate["sourceTree"]),
            candidate_deployment_plan_digest=_digest(candidate["deploymentPlan"]),
        )


def public_update_check_from_operation(
    *, kind: str, status: str, result: object
) -> UpdateCheckResult | None:
    """Project only the closed updateCheck member from an internal result.

    Internal analysis descriptors may coexist beside this member.  They are
    intentionally ignored instead of being copied to the tenant response.
    """

    if kind != "application.check-update" or status != "succeeded":
        return None
    if not isinstance(result, Mapping):
        return None
    try:
        return UpdateCheckResult.from_body(result.get("updateCheck"))
    except (TypeError, ValueError):
        return None


__all__ = ["UpdateCheckResult", "public_update_check_from_operation"]
