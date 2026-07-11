from __future__ import annotations

"""Internal placement admission for LAE tenant workloads.

LAE callers choose only a product region.  This module translates that intent
to an internal Nomad candidate set after checking Luma node readiness, runtime
capability, builder isolation, managed-volume reachability, and prior
placement.  Concrete node identities are deliberately confined to the
returned decision and the rendered Nomad job; ``safe_summary`` is suitable for
admin/audit presentation.
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


PLACEMENT_SCHEMA_VERSION = "luma.lae-placement/v1"

REASON_NO_CAPACITY = "no_capacity"
REASON_VOLUME_INCOMPATIBLE = "volume_incompatible"
REASON_UNAVAILABLE = "unavailable"


class PlacementFailure(Exception):
    """Stable, non-sensitive placement rejection consumed by Luma Control."""

    def __init__(self, reason: str) -> None:
        if reason not in {
            REASON_NO_CAPACITY,
            REASON_VOLUME_INCOMPATIBLE,
            REASON_UNAVAILABLE,
        }:
            reason = REASON_UNAVAILABLE
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class _Candidate:
    node_id: str
    registered_name: str
    aliases: frozenset[str]
    failure_domain_key: str = ""
    failure_domain: str = ""


@dataclass(frozen=True, slots=True)
class PlacementDecision:
    region: str
    requested_cpu_mhz: int
    requested_memory_mib: int
    stateful: bool
    candidate_node_ids: tuple[str, ...]
    preferred_node_id: str = ""
    preferred_failure_domain_key: str = ""
    preferred_failure_domain: str = ""
    continuity: str = "new"

    def safe_summary(self) -> dict[str, Any]:
        """Return a summary with no node, address, pool, or domain value."""

        summary: dict[str, Any] = {
            "schemaVersion": PLACEMENT_SCHEMA_VERSION,
            "region": self.region,
            "scheduler": "nomad",
            "candidateCount": len(self.candidate_node_ids),
            "requested": {
                "cpuMHz": self.requested_cpu_mhz,
                "memoryMiB": self.requested_memory_mib,
            },
            "stateful": self.stateful,
            "continuity": self.continuity,
        }
        # Bind the audit digest to the full internal decision without copying
        # any of that topology into the safe projection.
        digest_input = json.dumps(
            {
                "summary": summary,
                "candidateNodeIds": sorted(self.candidate_node_ids),
                "preferredNodeId": self.preferred_node_id,
                "preferredFailureDomainKey": (
                    self.preferred_failure_domain_key
                ),
                "preferredFailureDomain": self.preferred_failure_domain,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        summary["decisionDigest"] = (
            "sha256:" + hashlib.sha256(digest_input).hexdigest()
        )
        return summary

    def internal_state(self) -> dict[str, Any]:
        """Return the control-plane-only decision, plus its safe projection."""

        return {
            "schemaVersion": PLACEMENT_SCHEMA_VERSION,
            "candidateNodeIds": list(self.candidate_node_ids),
            **(
                {"preferredNodeId": self.preferred_node_id}
                if self.preferred_node_id
                else {}
            ),
            **(
                {
                    "preferredFailureDomain": {
                        "metaKey": self.preferred_failure_domain_key,
                        "value": self.preferred_failure_domain,
                    }
                }
                if self.preferred_failure_domain_key
                and self.preferred_failure_domain
                else {}
            ),
            "summary": self.safe_summary(),
        }

    def apply_to_job(self, job_document: dict[str, Any]) -> None:
        """Constrain Nomad to the admitted set while leaving selection to it."""

        job = job_document.get("Job")
        if not isinstance(job, dict) or not self.candidate_node_ids:
            raise PlacementFailure(REASON_UNAVAILABLE)
        constraints = job.get("Constraints")
        if not isinstance(constraints, list):
            constraints = []
            job["Constraints"] = constraints
        candidate_pattern = "^(?:" + "|".join(
            re.escape(value) for value in sorted(self.candidate_node_ids)
        ) + ")$"
        constraints.append(
            {
                "LTarget": "${node.unique.id}",
                "RTarget": candidate_pattern,
                "Operand": "regexp",
            }
        )
        affinities = job.get("Affinities")
        if not isinstance(affinities, list):
            affinities = []
            job["Affinities"] = affinities
        if self.preferred_node_id:
            affinities.append(
                {
                    "LTarget": "${node.unique.id}",
                    "RTarget": self.preferred_node_id,
                    "Operand": "=",
                    "Weight": 80,
                }
            )
        if self.preferred_failure_domain_key and self.preferred_failure_domain:
            affinities.append(
                {
                    "LTarget": "${meta."
                    + self.preferred_failure_domain_key
                    + "}",
                    "RTarget": self.preferred_failure_domain,
                    "Operand": "=",
                    "Weight": 40,
                }
            )
        if not affinities:
            job.pop("Affinities", None)


def plan_lae_placement(
    *,
    manifest: Mapping[str, Any],
    registered_nodes: Mapping[str, Any],
    nomad_nodes: Sequence[Mapping[str, Any]],
    declared_builder_nodes: Sequence[str] = (),
    allowed_runtime_nodes: Sequence[str] = (),
    storage_class: Mapping[str, Any] | None = None,
    prior_node_id: str = "",
    now: int | None = None,
    agent_stale_seconds: int = 120,
) -> PlacementDecision:
    """Build an internal placement decision from current Luma/Nomad truth."""

    region = str(manifest.get("region") or "")
    if region not in {"cn", "global"}:
        raise PlacementFailure(REASON_UNAVAILABLE)
    requested_cpu_mhz, requested_memory_mib = _requested_resources(manifest)
    stateful = bool(manifest.get("volumes"))
    current_time = int(time.time()) if now is None else int(now)
    builder_names = {
        str(value).strip()
        for value in declared_builder_nodes
        if str(value).strip()
    }
    runtime_names = {
        str(value).strip()
        for value in allowed_runtime_nodes
        if str(value).strip()
    }
    if not runtime_names:
        # Tenant workloads require positive admission. Generic Linux/Docker
        # capability is not proof that a node is an isolated LAE runner.
        raise PlacementFailure(REASON_UNAVAILABLE)

    all_candidates: list[_Candidate] = []
    prior_domain_key = ""
    prior_domain = ""
    for raw_node in nomad_nodes:
        node_id = str(raw_node.get("ID") or raw_node.get("Id") or "").strip()
        domain_key, domain = _failure_domain(raw_node)
        if node_id and node_id == prior_node_id:
            prior_domain_key, prior_domain = domain_key, domain
        record_entry = _registered_node_for_nomad(
            registered_nodes, raw_node
        )
        if record_entry is None or not node_id:
            continue
        registered_name, record, aliases = record_entry
        if not _nomad_node_ready(raw_node):
            continue
        if not _agent_runtime_ready(
            record,
            now=current_time,
            stale_seconds=agent_stale_seconds,
        ):
            continue
        if not _node_region_matches(raw_node, record, region):
            continue
        if _builder_only(
            registered_name,
            aliases,
            record,
            declared_builder_nodes=builder_names,
        ):
            continue
        if _control_plane_only(record):
            continue
        if not ({registered_name, *aliases} & runtime_names):
            continue
        all_candidates.append(
            _Candidate(
                node_id=node_id,
                registered_name=registered_name,
                aliases=frozenset(aliases),
                failure_domain_key=domain_key,
                failure_domain=domain,
            )
        )

    if not all_candidates:
        raise PlacementFailure(REASON_NO_CAPACITY)

    candidates = _volume_compatible_candidates(
        all_candidates,
        storage_class=storage_class,
        registered_nodes=registered_nodes,
        region=region,
        stateful=stateful,
    )
    if not candidates:
        raise PlacementFailure(REASON_VOLUME_INCOMPATIBLE)

    candidate_ids = tuple(sorted({candidate.node_id for candidate in candidates}))
    preferred_node_id = prior_node_id if prior_node_id in candidate_ids else ""
    preferred_domain_key = ""
    preferred_domain = ""
    if prior_domain_key and prior_domain and any(
        candidate.failure_domain_key == prior_domain_key
        and candidate.failure_domain == prior_domain
        for candidate in candidates
    ):
        preferred_domain_key = prior_domain_key
        preferred_domain = prior_domain
    continuity = (
        "preferred"
        if preferred_node_id
        else "rescheduled"
        if prior_node_id
        else "new"
    )
    return PlacementDecision(
        region=region,
        requested_cpu_mhz=requested_cpu_mhz,
        requested_memory_mib=requested_memory_mib,
        stateful=stateful,
        candidate_node_ids=candidate_ids,
        preferred_node_id=preferred_node_id,
        preferred_failure_domain_key=preferred_domain_key,
        preferred_failure_domain=preferred_domain,
        continuity=continuity,
    )


def validate_nomad_plan(plan: Any) -> None:
    """Fail closed when Nomad cannot place the rendered task group."""

    if not isinstance(plan, dict):
        raise PlacementFailure(REASON_UNAVAILABLE)
    failures = plan.get("FailedTGAllocs")
    if failures is None:
        return
    if not isinstance(failures, dict):
        raise PlacementFailure(REASON_UNAVAILABLE)
    if any(isinstance(value, dict) and bool(value) for value in failures.values()):
        # Nomad's detailed failure dimensions include node classes and capacity
        # topology.  They stay server-side; callers get one stable error.
        raise PlacementFailure(REASON_NO_CAPACITY)


def _requested_resources(manifest: Mapping[str, Any]) -> tuple[int, int]:
    services = manifest.get("services")
    if not isinstance(services, list) or not services:
        raise PlacementFailure(REASON_UNAVAILABLE)
    cpu_mhz = 0
    memory_mib = 0
    try:
        for service in services:
            if not isinstance(service, dict):
                raise ValueError
            resources = service.get("resources")
            if not isinstance(resources, dict):
                raise ValueError
            cpu_mhz += max(1, round(float(resources.get("cpu")) * 1000))
            memory = resources.get("memoryMiB")
            if isinstance(memory, bool):
                raise ValueError
            memory_mib += int(memory)
    except (TypeError, ValueError):
        raise PlacementFailure(REASON_UNAVAILABLE) from None
    if cpu_mhz <= 0 or memory_mib <= 0:
        raise PlacementFailure(REASON_UNAVAILABLE)
    return cpu_mhz, memory_mib


def _nomad_node_ready(node: Mapping[str, Any]) -> bool:
    status = str(node.get("Status") or node.get("status") or "").lower()
    eligibility = str(
        node.get("SchedulingEligibility")
        or node.get("schedulingEligibility")
        or ""
    ).lower()
    if status != "ready" or eligibility not in {"eligible", ""}:
        return False
    if bool(node.get("Drain") or node.get("drain")):
        return False
    drivers = node.get("Drivers")
    if isinstance(drivers, dict) and "docker" in drivers:
        docker = drivers.get("docker")
        if not isinstance(docker, dict):
            return False
        if docker.get("Detected") is False or docker.get("Healthy") is False:
            return False
    return True


def _agent_runtime_ready(
    record: Mapping[str, Any], *, now: int, stale_seconds: int
) -> bool:
    agent = record.get("agent")
    if not isinstance(agent, dict):
        return False
    status = str(agent.get("status") or "").lower()
    ready = status == "ready"
    if status == "online":
        last_seen = agent.get("lastSeen")
        ready = (
            isinstance(last_seen, int)
            and not isinstance(last_seen, bool)
            and 0 <= now - last_seen <= max(int(stale_seconds), 1)
        )
    if not ready:
        return False
    os_name = str(agent.get("os") or "").strip().lower()
    arch = str(agent.get("arch") or "").strip().lower()
    if os_name != "linux" or arch not in {"amd64", "x86_64"}:
        return False
    capabilities = {str(value) for value in agent.get("capabilities") or []}
    return bool(
        capabilities.intersection(
            {"docker-image", "docker-runtime", "lae-runtime"}
        )
    )


def _builder_only(
    name: str,
    aliases: set[str],
    record: Mapping[str, Any],
    *,
    declared_builder_nodes: set[str],
) -> bool:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    roles = {str(value).strip().lower() for value in record.get("roles") or []}
    roles.update(
        str(value).strip().lower()
        for value in (
            record.get("role"),
            labels.get("role"),
            labels.get("luma.role"),
            labels.get("luma.node.role"),
        )
        if value
    )
    explicit_runtime = _explicit_runtime(record, roles=roles)
    declared = bool(({name, *aliases}) & declared_builder_nodes)
    explicit_builder = bool(
        roles.intersection({"builder", "build"})
        or str(labels.get("luma.builder") or "").lower() == "true"
        or str(labels.get("role.builder") or "").lower() == "true"
    )
    return (declared or explicit_builder) and not explicit_runtime


def _control_plane_only(record: Mapping[str, Any]) -> bool:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    roles = {str(value).strip().lower() for value in record.get("roles") or []}
    roles.update(
        str(value).strip().lower()
        for value in (
            record.get("role"),
            labels.get("role"),
            labels.get("luma.role"),
            labels.get("luma.node.role"),
        )
        if value
    )
    manager = bool(
        str(record.get("status") or "").lower() == "manager"
        or str(record.get("nomadRole") or "").lower() == "server"
        or bool(record.get("nomadServer"))
        or str(labels.get("role.nomad-manager") or "").lower() == "true"
    )
    control_roles = {
        "control",
        "control-plane",
        "manager",
        "nomad-manager",
        "swarm-manager",
        "edge",
    }
    return (manager or bool(roles & control_roles)) and not _explicit_runtime(
        record, roles=roles
    )


def _explicit_runtime(
    record: Mapping[str, Any], *, roles: set[str] | None = None
) -> bool:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    role_values = roles or {
        str(value).strip().lower() for value in record.get("roles") or []
    }
    return bool(
        role_values.intersection(
            {"runtime", "lae-runtime"}
        )
        or str(labels.get("luma.runtime") or "").lower() == "true"
        or str(labels.get("role.runtime") or "").lower() == "true"
    )


def _registered_node_for_nomad(
    registered_nodes: Mapping[str, Any], nomad_node: Mapping[str, Any]
) -> tuple[str, Mapping[str, Any], set[str]] | None:
    node_id = str(nomad_node.get("ID") or nomad_node.get("Id") or "").strip()
    meta = nomad_node.get("Meta") if isinstance(nomad_node.get("Meta"), dict) else {}
    wanted_names = {
        str(nomad_node.get("Name") or "").strip(),
        str(meta.get("luma_node_name") or "").strip(),
    }
    records: list[
        tuple[str, Mapping[str, Any], set[str], set[str]]
    ] = []
    for key, raw_record in registered_nodes.items():
        if not isinstance(raw_record, dict):
            continue
        aliases = _registered_aliases(str(key), raw_record)
        record_labels = (
            raw_record.get("labels")
            if isinstance(raw_record.get("labels"), dict)
            else {}
        )
        ids = {
            str(raw_record.get("nodeId") or "").strip(),
            str(raw_record.get("nomadNodeId") or "").strip(),
            str(record_labels.get("luma.node.id") or "").strip(),
        }
        records.append((str(key), raw_record, aliases, ids))
    exact_names = {value for value in wanted_names if value}
    name_matches = [
        (key, record, aliases)
        for key, record, aliases, _ids in records
        if exact_names & aliases
    ]
    # Nomad's current luma_node_name/Name is stronger than a historical saved
    # node ID. This lets a uniquely named node survive an old duplicate-ID
    # registration without ever mapping that allocation to the other record.
    if len(name_matches) == 1:
        return name_matches[0]
    if len(name_matches) > 1:
        return None
    id_matches = [
        (key, record, aliases)
        for key, record, aliases, ids in records
        if node_id and node_id in ids
    ]
    # Ambiguous IDs are an integrity problem and must not be resolved by
    # dictionary order at a tenant scheduling boundary.
    return id_matches[0] if len(id_matches) == 1 else None


def _registered_aliases(key: str, record: Mapping[str, Any]) -> set[str]:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    aliases = {
        key,
        str(record.get("name") or "").strip(),
        str(record.get("displayName") or "").strip(),
        str(record.get("hostname") or "").strip(),
        str(labels.get("luma.node.name") or "").strip(),
        str(labels.get("luma_node_name") or "").strip(),
    }
    raw_aliases = record.get("aliases")
    if isinstance(raw_aliases, list):
        aliases.update(str(value).strip() for value in raw_aliases)
    elif isinstance(raw_aliases, str):
        aliases.add(raw_aliases.strip())
    # The manager's canonical registration may intentionally remain its host
    # name so existing Control/Nomad jobs do not need a disruptive rename.
    # Operators nevertheless need one stable, non-hostname selector when they
    # explicitly opt that node into LAE runtime service.  This alias is only a
    # name match: `_control_plane_only` still rejects the node unless its
    # record also carries an explicit runtime role/label.
    if (
        str(record.get("status") or "").strip().lower() == "manager"
        or str(record.get("nomadRole") or "").strip().lower() == "server"
        or bool(record.get("nomadServer"))
        or str(labels.get("role.nomad-manager") or "").strip().lower()
        == "true"
    ):
        aliases.add("manager")
    return {value for value in aliases if value}


def _node_region_matches(
    node: Mapping[str, Any], record: Mapping[str, Any], expected: str
) -> bool:
    meta = node.get("Meta") if isinstance(node.get("Meta"), dict) else {}
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    nomad_region = str(meta.get("region") or "").strip()
    registered_region = str(
        record.get("region") or labels.get("region") or ""
    ).strip()
    # Old registrations may lack one copy, but two conflicting sources must
    # never be resolved by precedence: that could cross a region boundary.
    values = {value for value in (nomad_region, registered_region) if value}
    return bool(values) and values == {expected}


def _failure_domain(node: Mapping[str, Any]) -> tuple[str, str]:
    meta = node.get("Meta") if isinstance(node.get("Meta"), dict) else {}
    for key in (
        "luma_failure_domain",
        "failure_domain",
        "failure-domain",
        "topology_zone",
    ):
        value = str(meta.get(key) or "").strip()
        if value:
            return key, value
    return "", ""


def _volume_compatible_candidates(
    candidates: list[_Candidate],
    *,
    storage_class: Mapping[str, Any] | None,
    registered_nodes: Mapping[str, Any],
    region: str,
    stateful: bool,
) -> list[_Candidate]:
    if not stateful:
        return candidates
    if not isinstance(storage_class, Mapping):
        raise PlacementFailure(REASON_VOLUME_INCOMPATIBLE)
    if (
        str(storage_class.get("provider") or "nfs") != "nfs"
        or str(storage_class.get("mode") or "managed") != "managed"
        or not str(storage_class.get("node") or "").strip()
        or not str(storage_class.get("path") or "").startswith("/")
    ):
        raise PlacementFailure(REASON_VOLUME_INCOMPATIBLE)
    regions = {str(value) for value in storage_class.get("regions") or []}
    if regions and region not in regions:
        raise PlacementFailure(REASON_VOLUME_INCOMPATIBLE)

    storage_node_name = str(storage_class.get("node") or "").strip()
    storage_record = registered_nodes.get(storage_node_name)
    if not isinstance(storage_record, dict):
        for key, value in registered_nodes.items():
            if isinstance(value, dict) and storage_node_name in _registered_aliases(
                str(key), value
            ):
                storage_record = value
                break
    if not isinstance(storage_record, dict):
        raise PlacementFailure(REASON_VOLUME_INCOMPATIBLE)
    labels = (
        storage_record.get("labels")
        if isinstance(storage_record.get("labels"), dict)
        else {}
    )
    storage_region = str(
        storage_record.get("region") or labels.get("region") or ""
    ).strip()
    if not storage_region:
        raise PlacementFailure(REASON_VOLUME_INCOMPATIBLE)
    if storage_region != region and not str(
        storage_record.get("tailscaleIP")
        or storage_record.get("tailscaleName")
        or ""
    ).strip():
        raise PlacementFailure(REASON_VOLUME_INCOMPATIBLE)

    allowed_nodes = {
        str(value).strip()
        for value in storage_class.get("nodes") or []
        if str(value).strip()
    }
    if allowed_nodes:
        candidates = [
            candidate
            for candidate in candidates
            if bool(
                ({candidate.registered_name, *candidate.aliases})
                & allowed_nodes
            )
        ]
    allowed_domains = {
        str(value).strip()
        for value in storage_class.get("failureDomains") or []
        if str(value).strip()
    }
    if allowed_domains:
        candidates = [
            candidate
            for candidate in candidates
            if candidate.failure_domain in allowed_domains
        ]
    return candidates


def safe_placement_json(value: PlacementDecision) -> str:
    """Deterministic helper for audit sinks; never serializes internal IDs."""

    return json.dumps(
        value.safe_summary(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
