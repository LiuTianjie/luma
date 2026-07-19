from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_ANALYSIS_ID = re.compile(r"^ana_[A-Za-z0-9][A-Za-z0-9._-]{2,60}$")
_CHANGE_KEY = re.compile(r"^[A-Za-z0-9_.*:@/+ -]{1,208}$")
_CHANGE_SECTIONS = ("services", "routes", "volumes", "environment")
_CONFIRMATION_CODES = frozenset(
    {
        "SERVICE_REMOVAL",
        "PUBLIC_ROUTE_CHANGE",
        "PERSISTENT_VOLUME_CHANGE",
        "REQUIRED_ENVIRONMENT_ADDED",
    }
)


def _digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("update-check digest is invalid")
    return value


def _change_key(value: object) -> str:
    if not isinstance(value, str) or _CHANGE_KEY.fullmatch(value) is None:
        raise ValueError("update-check change key is invalid")
    return value


@dataclass(frozen=True, slots=True)
class UpdateChangeSet:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for values in (self.added, self.removed, self.changed):
            if not isinstance(values, tuple):
                raise ValueError("update-check change lists must be tuples")
            normalized = tuple(_change_key(value) for value in values)
            if normalized != tuple(sorted(set(normalized))):
                raise ValueError("update-check change lists must be sorted and unique")
        if set(self.added) & set(self.removed):
            raise ValueError("update-check item cannot be added and removed")

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def to_body(self) -> dict[str, list[str]]:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "changed": list(self.changed),
        }

    @classmethod
    def from_body(cls, value: object) -> "UpdateChangeSet":
        if not isinstance(value, Mapping) or set(value) != {
            "added",
            "removed",
            "changed",
        }:
            raise ValueError("update-check change-set shape is invalid")
        parts: dict[str, tuple[str, ...]] = {}
        for name in ("added", "removed", "changed"):
            raw = value[name]
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
                raise ValueError("update-check change list is invalid")
            parts[name] = tuple(_change_key(item) for item in raw)
        return cls(**parts)


@dataclass(frozen=True, slots=True)
class UpdatePlanChanges:
    services: UpdateChangeSet = UpdateChangeSet()
    routes: UpdateChangeSet = UpdateChangeSet()
    volumes: UpdateChangeSet = UpdateChangeSet()
    environment: UpdateChangeSet = UpdateChangeSet()
    destructive: bool = False
    confirmations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.destructive, bool):
            raise ValueError("update-check destructive flag must be boolean")
        if not all(
            isinstance(getattr(self, section), UpdateChangeSet)
            for section in _CHANGE_SECTIONS
        ):
            raise ValueError("update-check sections are invalid")
        if (
            not isinstance(self.confirmations, tuple)
            or self.confirmations != tuple(sorted(set(self.confirmations)))
            or not set(self.confirmations) <= _CONFIRMATION_CODES
        ):
            raise ValueError("update-check confirmation codes are invalid")
        if self.destructive != bool(self.confirmations):
            raise ValueError("update-check destructive flag is inconsistent")

    @property
    def empty(self) -> bool:
        return all(getattr(self, section).empty for section in _CHANGE_SECTIONS)

    def to_body(self) -> dict[str, Any]:
        return {
            "destructive": self.destructive,
            "confirmations": list(self.confirmations),
            **{
                section: getattr(self, section).to_body()
                for section in _CHANGE_SECTIONS
            },
        }

    @classmethod
    def from_body(cls, value: object) -> "UpdatePlanChanges":
        expected = {"destructive", "confirmations", *_CHANGE_SECTIONS}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("update-check plan changes shape is invalid")
        confirmations = value["confirmations"]
        if not isinstance(confirmations, Sequence) or isinstance(
            confirmations, (str, bytes)
        ):
            raise ValueError("update-check confirmations are invalid")
        return cls(
            services=UpdateChangeSet.from_body(value["services"]),
            routes=UpdateChangeSet.from_body(value["routes"]),
            volumes=UpdateChangeSet.from_body(value["volumes"]),
            environment=UpdateChangeSet.from_body(value["environment"]),
            destructive=value["destructive"],
            confirmations=tuple(confirmations),
        )


def _plan_index(
    plan: Mapping[str, object], section: str, *, key: str
) -> dict[str, Mapping[str, object]]:
    if plan.get("schemaVersion") != "lae.deployment-plan/v1":
        raise ValueError("update-check deployment plan schema is invalid")
    raw = plan.get(section)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) > 256:
        raise ValueError("update-check deployment plan section is invalid")
    result: dict[str, Mapping[str, object]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("update-check deployment plan item is invalid")
        identifier = _change_key(item.get(key))
        if section == "environment":
            scope = _change_key(item.get("scope"))
            identifier = f"{scope}:{identifier}"
        if identifier in result:
            raise ValueError("update-check deployment plan key is duplicated")
        result[identifier] = item
    return result


def _diff_section(
    baseline: Mapping[str, object],
    candidate: Mapping[str, object],
    section: str,
    *,
    key: str,
) -> UpdateChangeSet:
    before = _plan_index(baseline, section, key=key)
    after = _plan_index(candidate, section, key=key)
    return UpdateChangeSet(
        added=tuple(sorted(after.keys() - before.keys())),
        removed=tuple(sorted(before.keys() - after.keys())),
        changed=tuple(
            sorted(
                identifier
                for identifier in before.keys() & after.keys()
                if before[identifier] != after[identifier]
            )
        ),
    )


def diff_deployment_plans(
    baseline: Mapping[str, object], candidate: Mapping[str, object]
) -> UpdatePlanChanges:
    """Return a closed, secret-free change summary for two verified plans."""

    services = _diff_section(baseline, candidate, "services", key="key")
    routes = _diff_section(baseline, candidate, "routes", key="serviceKey")
    volumes = _diff_section(baseline, candidate, "volumes", key="key")
    environment = _diff_section(
        baseline, candidate, "environment", key="name"
    )
    confirmations: set[str] = set()
    if services.removed:
        confirmations.add("SERVICE_REMOVAL")
    if routes.removed or routes.changed:
        confirmations.add("PUBLIC_ROUTE_CHANGE")
    if volumes.removed or volumes.changed:
        confirmations.add("PERSISTENT_VOLUME_CHANGE")

    candidate_environment = _plan_index(
        candidate, "environment", key="name"
    )
    if any(
        candidate_environment[key].get("required") is True
        for key in environment.added
    ):
        confirmations.add("REQUIRED_ENVIRONMENT_ADDED")
    return UpdatePlanChanges(
        services=services,
        routes=routes,
        volumes=volumes,
        environment=environment,
        destructive=bool(confirmations),
        confirmations=tuple(sorted(confirmations)),
    )


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
    plan_changes: UpdatePlanChanges | None = None
    candidate_analysis_id: str | None = None
    candidate_verdict: str | None = None

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
        if (self.candidate_analysis_id is None) != (
            self.candidate_verdict is None
        ):
            raise ValueError("update-check candidate analysis is incomplete")
        if self.candidate_analysis_id is not None and (
            _ANALYSIS_ID.fullmatch(self.candidate_analysis_id) is None
            or self.candidate_verdict
            not in {"deployable", "needs_input", "unsupported", "diagnostic_failed"}
        ):
            raise ValueError("update-check candidate analysis is invalid")
        if self.baseline_available:
            _digest(self.baseline_source_tree_digest)
            _digest(self.baseline_deployment_plan_digest)
            if self.changed != (
                self.source_changed or self.deployment_plan_changed
            ):
                raise ValueError("update-check changed flag is inconsistent")
            if self.plan_changes is not None:
                if not isinstance(self.plan_changes, UpdatePlanChanges):
                    raise ValueError("update-check plan changes are invalid")
                if self.deployment_plan_changed == self.plan_changes.empty:
                    raise ValueError("update-check plan changes are inconsistent")
        elif (
            self.baseline_source_tree_digest is not None
            or self.baseline_deployment_plan_digest is not None
            or not self.source_changed
            or not self.deployment_plan_changed
            or not self.changed
            or self.plan_changes is not None
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
            "candidateAnalysis": (
                {
                    "id": self.candidate_analysis_id,
                    "verdict": self.candidate_verdict,
                }
                if self.candidate_analysis_id is not None
                else None
            ),
            "changes": (
                self.plan_changes.to_body()
                if self.plan_changes is not None
                else None
            ),
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
        if not isinstance(value, Mapping) or set(value) not in ({
            "baselineAvailable",
            "sourceChanged",
            "deploymentPlanChanged",
            "changed",
            "digests",
        }, {
            "baselineAvailable",
            "sourceChanged",
            "deploymentPlanChanged",
            "changed",
            "changes",
            "digests",
        }, {
            "baselineAvailable",
            "sourceChanged",
            "deploymentPlanChanged",
            "changed",
            "changes",
            "candidateAnalysis",
            "digests",
        }):
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
        candidate_analysis = value.get("candidateAnalysis")
        candidate_analysis_id: str | None = None
        candidate_verdict: str | None = None
        if candidate_analysis is not None:
            if not isinstance(candidate_analysis, Mapping) or set(
                candidate_analysis
            ) != {"id", "verdict"}:
                raise ValueError("update-check candidate analysis shape is invalid")
            candidate_analysis_id = candidate_analysis["id"]
            candidate_verdict = candidate_analysis["verdict"]
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
            plan_changes=(
                UpdatePlanChanges.from_body(value["changes"])
                if value.get("changes") is not None
                else None
            ),
            candidate_analysis_id=candidate_analysis_id,
            candidate_verdict=candidate_verdict,
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


__all__ = [
    "UpdateChangeSet",
    "UpdatePlanChanges",
    "UpdateCheckResult",
    "diff_deployment_plans",
    "public_update_check_from_operation",
]
