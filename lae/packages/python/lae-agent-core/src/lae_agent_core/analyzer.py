from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

import yaml

from lae_contracts import is_safe_external_image_reference, validate_instance

from .ai import (
    AIDiagnosticError,
    AIControllerClientConfig,
    KNOWLEDGE_VERSION,
    apply_ai_proposal,
    build_ai_request,
    manifest_candidate_from_plan,
    request_ai_analysis,
    unsupported_findings,
)
from .canonical import atomic_write_json, canonical_bytes

AGENT_VERSION = "0.2.0"
ADAPTER_VERSION = "1.0.0"
MAX_ANALYZED_FILE_BYTES = 2 * 1024 * 1024
MAX_COMPOSE_BYTES = 2 * 1024 * 1024
DEFAULT_MEMORY_MIB = 512
DEFAULT_VOLUME_BYTES = 1024 * 1024 * 1024
COMPOSE_FILENAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)
IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}
TEXT_SUFFIXES = {
    ".cjs",
    ".cts",
    ".env",
    ".js",
    ".jsx",
    ".mjs",
    ".mts",
    ".py",
    ".ts",
    ".tsx",
}
DATASTORE_MARKERS = {
    "cassandra",
    "clickhouse",
    "couchdb",
    "elasticsearch",
    "mariadb",
    "memcached",
    "mongo",
    "mysql",
    "opensearch",
    "postgres",
    "postgresql",
    "rabbitmq",
    "redis",
    "valkey",
}
WORKER_MARKERS = {"beat", "consumer", "cron", "job", "queue", "scheduler", "worker"}
HTTP_MARKERS = {
    "admin",
    "api",
    "app",
    "backend",
    "dashboard",
    "frontend",
    "gateway",
    "proxy",
    "server",
    "ui",
    "web",
}
HTTP_PORTS = {80, 3000, 4173, 5000, 5173, 8000, 8080, 8081, 8888}
SENSITIVE_MARKERS = (
    "API_KEY",
    "AUTH",
    "CREDENTIAL",
    "DATABASE_URL",
    "PRIVATE_KEY",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)
PUBLIC_PREFIXES = ("NEXT_PUBLIC_", "NUXT_PUBLIC_", "PUBLIC_", "REACT_APP_", "VITE_")
FORBIDDEN_METADATA_KEY = re.compile(
    r"(?:authorization|credential|debug|log|password|secret|token)", re.IGNORECASE
)
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
COMMIT = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class AnalysisError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass
class EnvironmentFinding:
    name: str
    scope: str = "runtime"
    services: set[str] = field(default_factory=set)
    required: bool = False
    sensitive: bool = False
    public: bool = False
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ServiceContext:
    key: str
    context: str


class _LimitedSafeLoader(yaml.SafeLoader):
    """SafeLoader with a small alias budget to avoid YAML expansion attacks."""

    max_aliases = 100

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._alias_count = 0
        self._compose_depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            self._alias_count += 1
            if self._alias_count > self.max_aliases:
                raise yaml.YAMLError("Compose alias limit exceeded")
        self._compose_depth += 1
        try:
            if self._compose_depth > 100:
                raise yaml.YAMLError("Compose nesting limit exceeded")
            return super().compose_node(parent, index)
        finally:
            self._compose_depth -= 1

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            return super().construct_mapping(node, deep=deep)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.YAMLError("Compose mapping key is not scalar") from exc
            if duplicate:
                raise yaml.YAMLError("Compose contains a duplicate mapping key")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


# Compose uses YAML 1.2 boolean spellings. PyYAML's YAML 1.1 resolver would
# otherwise turn ordinary keys such as "on", "off", "yes", and "no" into bools.
_LimitedSafeLoader.yaml_implicit_resolvers = copy.deepcopy(
    yaml.SafeLoader.yaml_implicit_resolvers
)
for _resolver_key, _resolvers in list(
    _LimitedSafeLoader.yaml_implicit_resolvers.items()
):
    _LimitedSafeLoader.yaml_implicit_resolvers[_resolver_key] = [
        item for item in _resolvers if item[0] != "tag:yaml.org,2002:bool"
    ]
_LimitedSafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


class Analyzer:
    def __init__(self, source: Path, metadata: Mapping[str, Any]) -> None:
        self.source = source
        self.metadata = _validate_metadata(metadata)
        self.inventory: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.warnings: set[str] = set()
        self.blockers: set[str] = set()
        self.environments: dict[tuple[str, str], EnvironmentFinding] = {}
        self.services: list[dict[str, Any]] = []
        self.routes: list[dict[str, Any]] = []
        self.volumes: list[dict[str, Any]] = []
        self.builds: list[dict[str, Any]] = []
        self.external_images: list[dict[str, Any]] = []
        self.service_contexts: list[ServiceContext] = []
        self.kind = "service"
        self.adapter = "unknown"

    def run(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        self._inventory_source()
        compose_path = self._find_compose_file()
        if compose_path is not None:
            self._analyze_compose(compose_path)
        else:
            self._analyze_single_service()
        self._scan_environment_examples()
        self._scan_code_environment_references()
        self._attach_environment_names()
        self.warnings.add("BUILD_PLAN_REQUIRES_CONTROLLER_SIGNATURE")

        environment = [
            self._environment_payload(item) for item in self._sorted_environments()
        ]
        decision = (
            "deny"
            if self.blockers
            else (
                "needs_configuration"
                if any(item["required"] for item in environment)
                else "allow"
            )
        )
        seed = {
            # A snapshot id identifies one fetch attempt and is intentionally
            # different on every update check.  Plans describe source content,
            # so their identity must be derived from immutable source facts or
            # an unchanged repository will look like a deployment change.
            "metadata": {
                "resolvedCommit": self.metadata["resolvedCommit"],
                "sourceSnapshotDigest": self.metadata["sourceSnapshotDigest"],
                "policyVersion": self.metadata["policyVersion"],
            },
            "adapter": self.adapter,
            "inventory": self.inventory,
            "services": self.services,
            "builds": self.builds,
            "externalImages": self.external_images,
        }
        seed_digest = hashlib.sha256(canonical_bytes(seed)).hexdigest()
        deployment_plan = {
            "schemaVersion": "lae.deployment-plan/v1",
            "planId": f"plan_{seed_digest[:24]}",
            "sourceRevisionId": f"src_{_stable_identifier(self.metadata['sourceSnapshotDigest'])}",
            "sourceDigest": self.metadata["sourceSnapshotDigest"],
            "kind": self.kind,
            "services": sorted(self.services, key=lambda item: item["key"]),
            "routes": sorted(self.routes, key=lambda item: item["serviceKey"]),
            "volumes": sorted(self.volumes, key=lambda item: item["key"]),
            "environment": environment,
            "warnings": sorted(self.warnings),
            "blockers": sorted(self.blockers),
            "policy": {"version": self.metadata["policyVersion"], "decision": decision},
        }
        build_plan_proposal = {
            "schemaVersion": "lae.build-plan-proposal/v1",
            "sourceSnapshotDigest": self.metadata["sourceSnapshotDigest"],
            "resolvedCommit": self.metadata["resolvedCommit"],
            "policyVersion": self.metadata["policyVersion"],
            "builds": sorted(self.builds, key=lambda item: item["key"]),
            "externalImages": sorted(
                self.external_images, key=lambda item: item["key"]
            ),
        }
        evidence = {
            "schemaVersion": "lae.analysis-evidence/v1",
            "agentVersion": AGENT_VERSION,
            "adapter": {"name": self.adapter, "version": ADAPTER_VERSION},
            "source": {
                "resolvedCommit": self.metadata["resolvedCommit"],
                "sourceSnapshotId": self.metadata["sourceSnapshotId"],
                "sourceSnapshotDigest": self.metadata["sourceSnapshotDigest"],
            },
            "inventory": sorted(self.inventory, key=lambda item: item["path"]),
            "findings": sorted(
                self.evidence,
                key=lambda item: (
                    item.get("path", ""),
                    item.get("line", 0),
                    item.get("rule", ""),
                    item.get("name", ""),
                ),
            ),
            "environment": [
                self._evidence_environment_payload(item)
                for item in self._sorted_environments()
            ],
            "warnings": sorted(self.warnings),
            "blockers": sorted(self.blockers),
        }
        _validate_plan("deployment-plan.v1.schema.json", deployment_plan)
        _validate_plan("build-plan-proposal.v1.schema.json", build_plan_proposal)
        _validate_plan_semantics(deployment_plan, build_plan_proposal)
        return evidence, deployment_plan, build_plan_proposal

    def _inventory_source(self) -> None:
        for root, directories, filenames in os.walk(
            self.source, topdown=True, followlinks=False
        ):
            for directory in sorted(directories):
                directory_path = Path(root) / directory
                if directory_path.is_symlink():
                    relative = directory_path.relative_to(self.source).as_posix()
                    self._warning("SOURCE_SYMLINK_IGNORED", relative)
                    self.evidence.append(
                        {"path": relative, "rule": "source-symlink-ignored"}
                    )
            directories[:] = sorted(
                name
                for name in directories
                if name not in IGNORED_DIRECTORIES
                and not (Path(root) / name).is_symlink()
            )
            for filename in sorted(filenames):
                path = Path(root) / filename
                relative = path.relative_to(self.source).as_posix()
                try:
                    file_stat = path.lstat()
                except OSError:
                    self._warning("SOURCE_ENTRY_UNREADABLE", relative)
                    continue
                if stat.S_ISLNK(file_stat.st_mode):
                    self._warning("SOURCE_SYMLINK_IGNORED", relative)
                    self.evidence.append(
                        {"path": relative, "rule": "source-symlink-ignored"}
                    )
                    continue
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
                if _is_private_env_file(path.name):
                    self.evidence.append(
                        {"path": relative, "rule": "private-env-file-ignored"}
                    )
                    continue
                if file_stat.st_size > MAX_ANALYZED_FILE_BYTES:
                    self.inventory.append(
                        {
                            "path": relative,
                            "size": file_stat.st_size,
                            "rule": "oversize-file-not-read",
                        }
                    )
                    continue
                try:
                    content = path.read_bytes()
                except OSError:
                    self._warning("SOURCE_FILE_UNREADABLE", relative)
                    continue
                self.inventory.append(
                    {
                        "path": relative,
                        "size": len(content),
                        "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
                    }
                )

    def _find_compose_file(self) -> Path | None:
        matches: list[Path] = []
        for name in COMPOSE_FILENAMES:
            candidate = self.source / name
            if candidate.is_symlink():
                raise AnalysisError(
                    "LAE_COMPOSE_INVALID", "Compose document must not be a symlink"
                )
            if candidate.is_file():
                matches.append(candidate)
        if not matches:
            return None
        if len(matches) > 1:
            self._warning(
                "MULTIPLE_COMPOSE_FILES", matches[0].relative_to(self.source).as_posix()
            )
        return matches[0]

    def _analyze_compose(self, path: Path) -> None:
        self.kind = "compose"
        self.adapter = "compose"
        relative = path.relative_to(self.source).as_posix()
        if path.stat().st_size > MAX_COMPOSE_BYTES:
            raise AnalysisError(
                "LAE_COMPOSE_TOO_LARGE",
                "Compose document exceeds the analyzer size limit",
            )
        try:
            value = yaml.load(
                path.read_text(encoding="utf-8"), Loader=_LimitedSafeLoader
            )
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise AnalysisError(
                "LAE_COMPOSE_INVALID", "Compose document is not valid safe YAML"
            ) from exc
        if not isinstance(value, Mapping):
            raise AnalysisError(
                "LAE_COMPOSE_INVALID", "Compose document must be an object"
            )
        raw_services = value.get("services")
        if not isinstance(raw_services, Mapping) or not raw_services:
            raise AnalysisError(
                "LAE_COMPOSE_INVALID", "Compose services must be a non-empty object"
            )
        self.evidence.append(
            {"path": relative, "rule": "compose-services", "count": len(raw_services)}
        )
        self._inspect_top_level_compose(value, relative)

        key_map: dict[str, str] = {}
        used_keys: set[str] = set()
        normalized_owners: dict[str, str] = {}
        for raw_name in sorted(raw_services, key=str):
            if not isinstance(raw_name, str):
                self._block("COMPOSE_SERVICE_NAME_INVALID", relative, "services")
                continue
            normalized = _base_key(raw_name)
            if (
                normalized in normalized_owners
                and normalized_owners[normalized] != raw_name
            ):
                self._block("COMPOSE_SERVICE_KEY_COLLISION", relative, "services")
            normalized_owners.setdefault(normalized, raw_name)
            key_map[raw_name] = _unique_key(raw_name, used_keys)
        build_image_keys = self._compose_build_image_keys(
            relative,
            raw_services,
            key_map,
        )
        for raw_name in sorted(key_map):
            raw_service = raw_services[raw_name]
            if not isinstance(raw_service, Mapping):
                self._block("COMPOSE_SERVICE_INVALID", relative, f"services.{raw_name}")
                raw_service = {}
            self._analyze_compose_service(
                relative,
                raw_name,
                key_map[raw_name],
                raw_service,
                key_map,
                build_image_keys,
            )
        build_keys = {build["key"] for build in self.builds}
        for build in self.builds:
            build["dependsOnBuilds"] = sorted(
                dependency
                for dependency in build["dependsOnBuilds"]
                if dependency in build_keys and dependency != build["key"]
            )
        self._analyze_compose_volumes(relative, value, raw_services, key_map)
        self._ensure_unique_http_ports(relative)

    def _compose_build_image_keys(
        self,
        compose_path: str,
        raw_services: Mapping[str, Any],
        key_map: Mapping[str, str],
    ) -> dict[str, str]:
        """Map a Compose image tag to its unique in-repository build owner.

        Compose commonly builds one image and reuses that exact logical tag
        from workers or sidecars that only declare ``image:``.  Those consumers
        are build outputs, not external images that LAE should resolve or pull.
        """

        owners: dict[str, set[str]] = {}
        owner_fields: dict[str, list[str]] = {}
        for raw_name in sorted(key_map):
            raw_service = raw_services.get(raw_name)
            if not isinstance(raw_service, Mapping) or raw_service.get("build") is None:
                continue
            raw_image = raw_service.get("image")
            if raw_image is None:
                continue
            field = f"services.{raw_name}.image"
            if (
                not isinstance(raw_image, str)
                or not _is_safe_compose_build_image_reference(raw_image.strip())
            ):
                self._block("COMPOSE_BUILD_IMAGE_REFERENCE_INVALID", compose_path, field)
                continue
            image = raw_image.strip()
            owners.setdefault(image, set()).add(key_map[raw_name])
            owner_fields.setdefault(image, []).append(field)

        result: dict[str, str] = {}
        for image, build_keys in owners.items():
            if len(build_keys) == 1:
                result[image] = next(iter(build_keys))
                continue
            for field in owner_fields[image]:
                self._block("COMPOSE_BUILD_IMAGE_AMBIGUOUS", compose_path, field)
        return result

    def _inspect_top_level_compose(
        self, value: Mapping[str, Any], relative: str
    ) -> None:
        self._block_unknown_fields(
            value,
            {
                "configs",
                "name",
                "networks",
                "secrets",
                "services",
                "version",
                "volumes",
            },
            relative,
            "compose",
        )
        networks = value.get("networks")
        if isinstance(networks, Mapping):
            for name, network in networks.items():
                if isinstance(network, Mapping) and network.get("external"):
                    self._block(
                        "COMPOSE_EXTERNAL_NETWORK",
                        relative,
                        f"networks.{name}.external",
                    )
                if isinstance(network, Mapping) and network.get("ipam"):
                    self._block(
                        "COMPOSE_CUSTOM_IPAM", relative, f"networks.{name}.ipam"
                    )
                if isinstance(network, Mapping) and any(
                    network.get(field)
                    for field in ("driver", "driver_opts", "enable_ipv4", "enable_ipv6")
                ):
                    self._block("COMPOSE_CUSTOM_NETWORK", relative, f"networks.{name}")
        volumes = value.get("volumes")
        if isinstance(volumes, Mapping):
            for name, volume in volumes.items():
                if isinstance(volume, Mapping) and any(
                    volume.get(field) for field in ("driver", "driver_opts", "external")
                ):
                    self._block("COMPOSE_EXTERNAL_VOLUME", relative, f"volumes.{name}")
        for section in ("configs", "secrets"):
            entries = value.get(section)
            if not isinstance(entries, Mapping):
                continue
            for name, entry in entries.items():
                if isinstance(entry, Mapping) and "file" in entry:
                    candidate = entry.get("file")
                    if (
                        not isinstance(candidate, str)
                        or not _is_safe_source_path(candidate)
                        or not _is_safe_source_entry(
                            self.source, candidate, directory=False
                        )
                    ):
                        self._block(
                            "COMPOSE_SOURCE_PATH_ESCAPE",
                            relative,
                            f"{section}.{name}.file",
                        )

    def _analyze_compose_service(
        self,
        compose_path: str,
        raw_name: str,
        key: str,
        value: Mapping[str, Any],
        key_map: Mapping[str, str],
        build_image_keys: Mapping[str, str],
    ) -> None:
        prefix = f"services.{raw_name}"
        self._inspect_compose_security(compose_path, prefix, value)
        labels = _compose_labels(value.get("labels"))
        role = _classify_compose_role(raw_name, value, labels)
        dependency_names = _compose_dependency_names(value.get("depends_on"))
        for dependency_name in dependency_names:
            if dependency_name not in key_map:
                self._block(
                    "COMPOSE_DEPENDENCY_UNKNOWN", compose_path, f"{prefix}.depends_on"
                )
            elif dependency_name == raw_name:
                self._block(
                    "COMPOSE_DEPENDENCY_SELF", compose_path, f"{prefix}.depends_on"
                )
        dependencies = {
            key_map[name]
            for name in dependency_names
            if name in key_map and name != raw_name
        }
        if isinstance(value.get("depends_on"), Mapping):
            for dependency_name, condition in value["depends_on"].items():
                if (
                    isinstance(condition, Mapping)
                    and condition.get("condition") == "service_completed_successfully"
                ):
                    self._block(
                        "COMPOSE_STRICT_START_ORDER_UNSUPPORTED",
                        compose_path,
                        f"{prefix}.depends_on.{dependency_name}.condition",
                    )
        image, build = self._compose_image_and_build(
            compose_path,
            prefix,
            key,
            value,
            build_image_keys,
        )
        if build is not None:
            self.builds.append(build)
            self.service_contexts.append(
                ServiceContext(key=key, context=build["context"])
            )
        elif image["source"] == "build":
            self.evidence.append(
                {
                    "path": compose_path,
                    "rule": "compose-shared-build-image",
                    "name": key,
                    "buildKey": image["buildKey"],
                }
            )
        elif image["source"] == "external" and is_safe_external_image_reference(
            image["ref"]
        ):
            external_image = {
                "key": key,
                "ref": image["ref"],
                "platform": "linux/amd64",
            }
            if "@" in image["ref"]:
                external_image["resolvedDigest"] = image["ref"].rsplit("@", 1)[1]
            self.external_images.append(external_image)

        port, protocol = self._compose_service_port(value, labels)
        if (
            protocol in {"tcp", "udp"}
            and labels.get("lae.public.protocol", "http").lower() != "http"
        ):
            self._block(
                f"PUBLIC_{protocol.upper()}_UNSUPPORTED",
                compose_path,
                f"{prefix}.labels",
            )
        if protocol == "udp":
            self._block("PUBLIC_UDP_UNSUPPORTED", compose_path, f"{prefix}.expose")
        if role == "http" and port is None:
            port = _default_port_for_service(value, image)
            self._warning("HTTP_PORT_INFERRED", f"{compose_path}#{prefix}")
        if role != "http" and labels.get("lae.public", "false").lower() == "true":
            self._block("PUBLIC_NON_HTTP_UNSUPPORTED", compose_path, f"{prefix}.labels")

        environment_names = self._compose_environment(compose_path, prefix, key, value)
        cpu, memory = _compose_resources(value)
        command = None
        if value.get("command") is not None:
            self.evidence.append(
                {"path": compose_path, "rule": "compose-command-present", "name": key}
            )
            self._warning(
                "COMMAND_REQUIRES_CONTROLLER_NORMALIZATION", f"{compose_path}#{prefix}"
            )
        service: dict[str, Any] = {
            "key": key,
            "role": role,
            "image": image,
            "command": command,
            "dependencies": sorted(dependencies),
            "environmentNames": sorted(environment_names),
            "resources": {"cpu": cpu, "memoryMiB": memory},
        }
        if port is not None:
            service["port"] = port
        if role == "http" and port is not None:
            health_path = labels.get("lae.health.path", "/healthz")
            if not health_path.startswith("/"):
                health_path = "/healthz"
                self._warning("HEALTH_PATH_INVALID", f"{compose_path}#{prefix}")
            service["healthcheck"] = {
                "type": "http",
                "path": health_path,
                "intervalSeconds": 10,
            }
            self.routes.append(
                {
                    "serviceKey": key,
                    "kind": "http",
                    "primary": False,
                    "hostnameRef": f"domain_{key}",
                    "containerPort": port,
                    "healthPath": health_path,
                }
            )
        self.services.append(service)
        self.evidence.append(
            {"path": compose_path, "rule": "compose-service", "name": key, "role": role}
        )
        if self.routes:
            primary_key = min(route["serviceKey"] for route in self.routes)
            for route in self.routes:
                route["primary"] = route["serviceKey"] == primary_key

    def _inspect_compose_security(
        self, compose_path: str, prefix: str, value: Mapping[str, Any]
    ) -> None:
        self._block_unknown_fields(
            value,
            {
                "build",
                "command",
                "depends_on",
                "deploy",
                "environment",
                "env_file",
                "expose",
                "healthcheck",
                "image",
                "labels",
                "ports",
                "platform",
                "restart",
                "volumes",
            },
            compose_path,
            prefix,
        )
        denied_truthy = {
            "privileged": "COMPOSE_PRIVILEGED",
            "devices": "COMPOSE_DEVICES",
            "cap_add": "COMPOSE_CAP_ADD",
            "security_opt": "COMPOSE_SECURITY_OVERRIDE",
            "sysctls": "COMPOSE_SYSCTLS",
            "ulimits": "COMPOSE_ULIMITS",
            "mac_address": "COMPOSE_MAC_ADDRESS",
        }
        for field_name, code in denied_truthy.items():
            if value.get(field_name):
                self._block(code, compose_path, f"{prefix}.{field_name}")
        for namespace in ("network_mode", "pid", "ipc"):
            raw = value.get(namespace)
            if isinstance(raw, str) and raw.lower() == "host":
                self._block(
                    f"COMPOSE_{namespace.upper()}_HOST",
                    compose_path,
                    f"{prefix}.{namespace}",
                )
        platform = value.get("platform")
        if platform is not None and platform != "linux/amd64":
            self._block(
                "COMPOSE_PLATFORM_UNSUPPORTED", compose_path, f"{prefix}.platform"
            )
        for index, published_port in enumerate(_as_list(value.get("ports"))):
            field_path = f"{prefix}.ports[{index}]"
            if _port_has_explicit_host_publish(published_port):
                self._block("COMPOSE_HOST_PORT", compose_path, field_path)
            parsed_port = _published_container_port(published_port)
            if parsed_port is not None and parsed_port[1] != "tcp":
                self._block(
                    f"PUBLIC_{parsed_port[1].upper()}_UNSUPPORTED",
                    compose_path,
                    field_path,
                )
        volumes = value.get("volumes")
        if isinstance(volumes, Sequence) and not isinstance(volumes, (str, bytes)):
            for index, volume in enumerate(volumes):
                source, target, volume_type = _compose_volume_parts(volume)
                field_path = f"{prefix}.volumes[{index}]"
                if _contains_docker_socket(source) or _contains_docker_socket(target):
                    self._block("COMPOSE_DOCKER_SOCKET", compose_path, field_path)
                if target is not None and target.startswith(("/dev", "/proc", "/sys")):
                    self._block(
                        "COMPOSE_HOST_NAMESPACE_MOUNT", compose_path, field_path
                    )
                if volume_type == "bind" or (
                    source is not None and _looks_like_host_path(source)
                ):
                    self._block("COMPOSE_HOST_BIND", compose_path, field_path)
                if source is None:
                    self._block("COMPOSE_ANONYMOUS_VOLUME", compose_path, field_path)
                if isinstance(volume, Mapping) and any(
                    volume.get(field) for field in ("bind", "consistency", "volume")
                ):
                    self._block(
                        "COMPOSE_VOLUME_OPTION_UNSUPPORTED", compose_path, field_path
                    )
        build = value.get("build")
        if isinstance(build, Mapping):
            self._block_unknown_fields(
                build,
                {"args", "context", "dockerfile", "secrets", "target"},
                compose_path,
                f"{prefix}.build",
            )
            for field_name in (
                "additional_contexts",
                "cache_from",
                "cache_to",
                "network",
                "privileged",
                "ssh",
            ):
                if build.get(field_name):
                    self._block(
                        "COMPOSE_BUILD_OPTION_UNSUPPORTED",
                        compose_path,
                        f"{prefix}.build.{field_name}",
                    )
        networks = value.get("networks")
        if isinstance(networks, Mapping):
            for network_name, network in networks.items():
                if isinstance(network, Mapping) and any(
                    network.get(field)
                    for field in ("ipv4_address", "ipv6_address", "link_local_ips")
                ):
                    self._block(
                        "COMPOSE_STATIC_IP",
                        compose_path,
                        f"{prefix}.networks.{network_name}",
                    )
        labels = _compose_labels(value.get("labels"))
        for label_name in labels:
            if label_name.lower().startswith(
                ("traefik.", "luma.", "nomad.", "com.hashicorp.nomad")
            ):
                self._block("COMPOSE_PLATFORM_LABEL", compose_path, f"{prefix}.labels")
        deploy = value.get("deploy")
        if isinstance(deploy, Mapping):
            self._block_unknown_fields(
                deploy,
                {"replicas", "resources"},
                compose_path,
                f"{prefix}.deploy",
            )
            replicas = deploy.get("replicas", 1)
            if replicas not in (None, 1, "1"):
                self._block(
                    "COMPOSE_REPLICAS_UNSUPPORTED",
                    compose_path,
                    f"{prefix}.deploy.replicas",
                )
            if any(
                deploy.get(field) for field in ("mode", "placement", "endpoint_mode")
            ):
                self._block(
                    "COMPOSE_PLACEMENT_UNSUPPORTED", compose_path, f"{prefix}.deploy"
                )
            resources = deploy.get("resources")
            if isinstance(resources, Mapping):
                self._block_unknown_fields(
                    resources,
                    {"limits"},
                    compose_path,
                    f"{prefix}.deploy.resources",
                )
                limits = resources.get("limits")
                if isinstance(limits, Mapping):
                    self._block_unknown_fields(
                        limits,
                        {"cpus", "memory"},
                        compose_path,
                        f"{prefix}.deploy.resources.limits",
                    )
                elif limits is not None:
                    self._block(
                        "COMPOSE_DEPLOY_RESOURCES_INVALID",
                        compose_path,
                        f"{prefix}.deploy.resources.limits",
                    )
            elif resources is not None:
                self._block(
                    "COMPOSE_DEPLOY_RESOURCES_INVALID",
                    compose_path,
                    f"{prefix}.deploy.resources",
                )
        elif deploy is not None:
            self._block("COMPOSE_DEPLOY_INVALID", compose_path, f"{prefix}.deploy")
        for env_file_index, env_file in enumerate(_as_list(value.get("env_file"))):
            if isinstance(env_file, Mapping):
                env_file = env_file.get("path")
            if not isinstance(env_file, str) or not _is_safe_source_path(env_file):
                self._block(
                    "COMPOSE_SOURCE_PATH_ESCAPE",
                    compose_path,
                    f"{prefix}.env_file[{env_file_index}]",
                )
            elif not _is_safe_source_entry(self.source, env_file, directory=False):
                self._block(
                    "COMPOSE_SOURCE_PATH_INVALID",
                    compose_path,
                    f"{prefix}.env_file[{env_file_index}]",
                )

    def _compose_image_and_build(
        self,
        compose_path: str,
        prefix: str,
        key: str,
        value: Mapping[str, Any],
        build_image_keys: Mapping[str, str],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        raw_build = value.get("build")
        if raw_build is not None:
            if isinstance(raw_build, str):
                context = raw_build
                build_options: Mapping[str, Any] = {}
            elif isinstance(raw_build, Mapping):
                context_value = raw_build.get("context", ".")
                context = context_value if isinstance(context_value, str) else "."
                build_options = raw_build
            else:
                self._block("COMPOSE_BUILD_INVALID", compose_path, f"{prefix}.build")
                context = "."
                build_options = {}
            normalized_context = _normalize_source_path(context)
            if normalized_context is None:
                self._block(
                    "COMPOSE_BUILD_CONTEXT_ESCAPE",
                    compose_path,
                    f"{prefix}.build.context",
                )
                normalized_context = "."
            elif not _is_safe_source_entry(
                self.source, normalized_context, directory=True
            ):
                self._block(
                    "COMPOSE_BUILD_CONTEXT_INVALID",
                    compose_path,
                    f"{prefix}.build.context",
                )
            raw_dockerfile = build_options.get("dockerfile", "Dockerfile")
            dockerfile_name = (
                raw_dockerfile if isinstance(raw_dockerfile, str) else "Dockerfile"
            )
            dockerfile = _join_source_path(normalized_context, dockerfile_name)
            if dockerfile is None:
                self._block(
                    "COMPOSE_DOCKERFILE_ESCAPE",
                    compose_path,
                    f"{prefix}.build.dockerfile",
                )
                dockerfile = "Dockerfile"
            elif not _is_safe_source_entry(self.source, dockerfile, directory=False):
                self._block(
                    "COMPOSE_DOCKERFILE_INVALID",
                    compose_path,
                    f"{prefix}.build.dockerfile",
                )
            build_arg_names = _name_list(build_options.get("args"))
            invalid_args = [
                name for name in build_arg_names if not ENV_NAME.fullmatch(name)
            ]
            if invalid_args:
                self._block(
                    "BUILD_ARG_NAME_INVALID", compose_path, f"{prefix}.build.args"
                )
            build_arg_names = [
                name for name in build_arg_names if ENV_NAME.fullmatch(name)
            ]
            secret_mount_names = _build_secret_names(build_options.get("secrets"))
            invalid_secrets = [
                name for name in secret_mount_names if not ENV_NAME.fullmatch(name)
            ]
            if invalid_secrets:
                self._block(
                    "BUILD_SECRET_NAME_INVALID", compose_path, f"{prefix}.build.secrets"
                )
            secret_mount_names = [
                name for name in secret_mount_names if ENV_NAME.fullmatch(name)
            ]
            target = build_options.get("target")
            if isinstance(target, str) and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", target
            ):
                self._block(
                    "COMPOSE_BUILD_TARGET_INVALID",
                    compose_path,
                    f"{prefix}.build.target",
                )
                target = None
            elif not isinstance(target, str):
                target = None
            build = {
                "key": key,
                "context": normalized_context,
                "dockerfile": dockerfile,
                "target": target,
                "platform": "linux/amd64",
                "buildArgNames": sorted(set(build_arg_names)),
                "secretMountNames": sorted(set(secret_mount_names)),
                # Compose depends_on is a runtime relationship, not a build DAG.
                "dependsOnBuilds": [],
            }
            for name in build["buildArgNames"]:
                self._add_environment(
                    name,
                    "build",
                    {key},
                    required=True,
                    path=compose_path,
                    rule="compose-build-arg",
                )
            for name in build["secretMountNames"]:
                self._add_environment(
                    name,
                    "build",
                    {key},
                    required=True,
                    path=compose_path,
                    rule="compose-build-secret",
                    force_sensitive=True,
                )
            return {"source": "build", "buildKey": key}, build
        raw_image = value.get("image")
        if isinstance(raw_image, str):
            build_key = build_image_keys.get(raw_image.strip())
            if build_key is not None:
                return {"source": "build", "buildKey": build_key}, None
        if isinstance(raw_image, str) and _is_safe_image_reference(raw_image.strip()):
            return {"source": "external", "ref": raw_image.strip()}, None
        if isinstance(raw_image, str) and raw_image.strip():
            self._block(
                "COMPOSE_IMAGE_REFERENCE_INVALID", compose_path, f"{prefix}.image"
            )
            return {"source": "external", "ref": "scratch"}, None
        self._block("COMPOSE_IMAGE_OR_BUILD_REQUIRED", compose_path, f"{prefix}.image")
        return {"source": "external", "ref": "scratch"}, None

    def _compose_service_port(
        self, value: Mapping[str, Any], labels: Mapping[str, str]
    ) -> tuple[int | None, str | None]:
        label_port = labels.get("lae.http.port")
        if label_port and label_port.isdigit() and 1 <= int(label_port) <= 65535:
            return int(label_port), "http"
        candidates: list[tuple[int, str]] = []
        for raw in _as_list(value.get("expose")):
            parsed = _container_port(raw)
            if parsed is not None:
                candidates.append(parsed)
        if not candidates:
            for raw in _as_list(value.get("ports")):
                parsed = _published_container_port(raw)
                if parsed is not None:
                    candidates.append(parsed)
        if not candidates:
            return None, None
        candidates.sort(key=lambda item: (item[0] not in HTTP_PORTS, item[0], item[1]))
        return candidates[0]

    def _compose_environment(
        self, compose_path: str, prefix: str, key: str, value: Mapping[str, Any]
    ) -> set[str]:
        names: set[str] = set()
        raw_environment = value.get("environment")
        if isinstance(raw_environment, Mapping):
            entries = raw_environment.items()
        elif isinstance(raw_environment, Sequence) and not isinstance(
            raw_environment, (str, bytes)
        ):
            normalized: list[tuple[Any, Any]] = []
            for item in raw_environment:
                if isinstance(item, str):
                    name, separator, expression = item.partition("=")
                    normalized.append((name, expression if separator else None))
            entries = normalized
        else:
            entries = []
        for raw_name, expression in entries:
            if not isinstance(raw_name, str) or not ENV_NAME.fullmatch(raw_name):
                self._block(
                    "ENVIRONMENT_NAME_INVALID", compose_path, f"{prefix}.environment"
                )
                continue
            # The analyzer never copies Compose values into durable artifacts,
            # including apparently non-sensitive literals.  Every Compose env
            # key therefore requires an explicit LAE environment value before
            # deploy; otherwise a plan could be marked deployable while silently
            # dropping configuration such as APP_MODE=production.
            required = True
            if (
                _is_sensitive_name(raw_name)
                and isinstance(expression, str)
                and "$" not in expression
            ):
                self._warning(
                    "HARDCODED_SENSITIVE_ENV_REQUIRES_REPLACEMENT",
                    f"{compose_path}#{prefix}",
                )
            self._add_environment(
                raw_name,
                "runtime",
                {key},
                required=required,
                path=compose_path,
                rule="compose-environment",
            )
            names.add(raw_name)
        for env_file in _as_list(value.get("env_file")):
            if isinstance(env_file, Mapping):
                env_file = env_file.get("path")
            if not isinstance(env_file, str) or not _is_safe_source_path(env_file):
                continue
            path = self.source / env_file
            if _is_example_env_file(path.name) and _is_safe_source_entry(
                self.source, env_file, directory=False
            ):
                names.update(self._read_env_example(path, {key}))
            else:
                self._warning("ENV_FILE_NOT_READ", f"{compose_path}#{prefix}.env_file")
        return names

    def _analyze_compose_volumes(
        self,
        compose_path: str,
        value: Mapping[str, Any],
        raw_services: Mapping[str, Any],
        key_map: Mapping[str, str],
    ) -> None:
        top_volumes = value.get("volumes")
        if not isinstance(top_volumes, Mapping):
            top_volumes = {}
        volume_uses: dict[str, list[tuple[str, str]]] = {}
        for raw_name, raw_service in raw_services.items():
            if raw_name not in key_map or not isinstance(raw_service, Mapping):
                continue
            for volume in _as_list(raw_service.get("volumes")):
                source, target, volume_type = _compose_volume_parts(volume)
                if source is None or target is None or volume_type == "bind":
                    continue
                if source in top_volumes or not _looks_like_host_path(source):
                    volume_uses.setdefault(source, []).append(
                        (key_map[raw_name], target)
                    )
        used_keys: set[str] = set()
        for raw_name in sorted(volume_uses):
            uses = volume_uses[raw_name]
            targets = sorted({target for _, target in uses})
            if not targets:
                continue
            if len(targets) > 1:
                self._block(
                    "VOLUME_MOUNT_PATH_CONFLICT", compose_path, f"volumes.{raw_name}"
                )
            key = _unique_key(raw_name, used_keys)
            self.volumes.append(
                {
                    "key": key,
                    "serviceKeys": sorted({service for service, _ in uses}),
                    "mountPath": targets[0],
                    "class": "persistent",
                    "requestedBytes": DEFAULT_VOLUME_BYTES,
                    "accessMode": "ReadWriteOnce",
                    "backupPolicy": "plan-default",
                    "deletePolicy": "retain",
                }
            )
            self.evidence.append(
                {"path": compose_path, "rule": "compose-named-volume", "name": key}
            )

    def _ensure_unique_http_ports(self, compose_path: str) -> None:
        owners: dict[int, str] = {}
        for service in self.services:
            port = service.get("port")
            if not isinstance(port, int):
                continue
            owner = owners.get(port)
            if owner is not None:
                self._block(
                    "COMPOSE_SHARED_NETWORK_PORT_CONFLICT",
                    compose_path,
                    f"services.{owner},{service['key']}.port",
                )
            else:
                owners[port] = service["key"]

    def _analyze_single_service(self) -> None:
        dockerfiles = sorted(
            item["path"]
            for item in self.inventory
            if PurePosixPath(item["path"]).name.lower() == "dockerfile"
        )
        root_dockerfile = "Dockerfile" if "Dockerfile" in dockerfiles else None
        dockerfile = root_dockerfile or (dockerfiles[0] if dockerfiles else None)
        package_path = self.source / "package.json"
        has_package = _is_safe_source_entry(
            self.source, "package.json", directory=False
        )
        python_project = any(
            _is_safe_source_entry(self.source, name, directory=False)
            for name in ("pyproject.toml", "requirements.txt", "Pipfile")
        )
        index_files = sorted(
            item["path"]
            for item in self.inventory
            if PurePosixPath(item["path"]).name.lower() == "index.html"
        )
        key = "web"
        port: int | None = None
        role = "worker"
        build_args: list[str] = []
        build_secrets: list[str] = []
        if dockerfile is not None:
            self.adapter = "dockerfile"
            dockerfile_data = self._read_source_text(dockerfile)
            docker_ports = _dockerfile_ports(dockerfile_data)
            if any(protocol == "udp" for _, protocol in docker_ports):
                self._block("PUBLIC_UDP_UNSUPPORTED", dockerfile, "EXPOSE")
            port = _select_http_port(
                [candidate for candidate, protocol in docker_ports if protocol == "tcp"]
            )
            build_args = sorted(_dockerfile_arg_names(dockerfile_data))
            build_secrets = sorted(_dockerfile_secret_names(dockerfile_data))
            if port is not None or has_package or python_project:
                role = "http"
                port = port or (3000 if has_package else 8000)
            self.evidence.append({"path": dockerfile, "rule": "dockerfile"})
        elif has_package:
            self.adapter = "node-http"
            role = "http"
            port = _node_default_port(package_path)
            dockerfile = ".lae/adapters/node-v1.Dockerfile"
            self.evidence.append({"path": "package.json", "rule": "node-project"})
            self.warnings.add(
                "PLATFORM_DOCKERFILE_REQUIRED:.lae/adapters/node-v1.Dockerfile"
            )
        elif python_project:
            self.adapter = "python-http"
            role = "http"
            port = 8000
            dockerfile = ".lae/adapters/python-v1.Dockerfile"
            evidence_path = next(
                name
                for name in ("pyproject.toml", "requirements.txt", "Pipfile")
                if (self.source / name).is_file()
            )
            self.evidence.append({"path": evidence_path, "rule": "python-project"})
            self.warnings.add(
                "PLATFORM_DOCKERFILE_REQUIRED:.lae/adapters/python-v1.Dockerfile"
            )
        elif index_files:
            self.adapter = "static-html"
            role = "http"
            port = 8080
            dockerfile = ".lae/adapters/static-v1.Dockerfile"
            self.evidence.append({"path": index_files[0], "rule": "static-entrypoint"})
            self.warnings.add(
                "PLATFORM_DOCKERFILE_REQUIRED:.lae/adapters/static-v1.Dockerfile"
            )
        else:
            self.adapter = "unknown"
            dockerfile = ".lae/adapters/unsupported.Dockerfile"
            self._block("SOURCE_KIND_UNSUPPORTED", ".", "detector")

        for name in build_args:
            if ENV_NAME.fullmatch(name):
                self._add_environment(
                    name, "build", {key}, False, dockerfile, "dockerfile-arg"
                )
            else:
                self._block("BUILD_ARG_NAME_INVALID", dockerfile, "ARG")
        for name in build_secrets:
            if ENV_NAME.fullmatch(name):
                self._add_environment(
                    name,
                    "build",
                    {key},
                    False,
                    dockerfile,
                    "dockerfile-secret",
                    force_sensitive=True,
                )
            else:
                self._block("BUILD_SECRET_NAME_INVALID", dockerfile, "RUN--mount")
        build = {
            "key": key,
            "context": ".",
            "dockerfile": dockerfile,
            "target": None,
            "platform": "linux/amd64",
            "buildArgNames": [name for name in build_args if ENV_NAME.fullmatch(name)],
            "secretMountNames": [
                name for name in build_secrets if ENV_NAME.fullmatch(name)
            ],
            "dependsOnBuilds": [],
        }
        self.builds.append(build)
        self.service_contexts.append(ServiceContext(key=key, context="."))
        service: dict[str, Any] = {
            "key": key,
            "role": role,
            "image": {"source": "build", "buildKey": key},
            "command": None,
            "dependencies": [],
            "environmentNames": [],
            "resources": {"cpu": "0.50", "memoryMiB": DEFAULT_MEMORY_MIB},
        }
        if port is not None:
            service["port"] = port
        if role == "http" and port is not None:
            health_path = "/healthz" if self.adapter == "static-html" else "/"
            service["healthcheck"] = {
                "type": "http",
                "path": health_path,
                "intervalSeconds": 10,
            }
            self.routes.append(
                {
                    "serviceKey": key,
                    "kind": "http",
                    "primary": True,
                    "hostnameRef": f"domain_{key}",
                    "containerPort": port,
                    "healthPath": health_path,
                }
            )
        self.services.append(service)

    def _scan_environment_examples(self) -> None:
        targets = {
            service["key"]
            for service in self.services
            if service["role"] != "datastore"
        }
        if not targets:
            targets = {service["key"] for service in self.services}
        for item in self.inventory:
            relative = item["path"]
            path = self.source / relative
            if _is_example_env_file(path.name):
                self._read_env_example(path, targets)

    def _read_env_example(self, path: Path, services: set[str]) -> set[str]:
        names: set[str] = set()
        relative = path.relative_to(self.source).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            self._warning("ENV_EXAMPLE_UNREADABLE", relative)
            return names
        for line_number, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[7:].lstrip()
            name, separator, raw_value = stripped.partition("=")
            name = name.strip()
            if not separator or not ENV_NAME.fullmatch(name):
                continue
            required = not raw_value.strip() or raw_value.strip().lower() in {
                "change-me",
                "changeme",
                "required",
                "your-value-here",
            }
            self._add_environment(
                name,
                "runtime",
                services,
                required,
                relative,
                "env-example-key",
                line=line_number,
            )
            names.add(name)
        return names

    def _scan_code_environment_references(self) -> None:
        patterns = (
            (re.compile(r"\bprocess\.env\.([A-Z][A-Z0-9_]*)\b"), "process-env"),
            (
                re.compile(r"\bprocess\.env\[['\"]([A-Z][A-Z0-9_]*)['\"]\]"),
                "process-env-index",
            ),
            (
                re.compile(r"\bos\.getenv\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
                "python-os-getenv",
            ),
            (
                re.compile(
                    r"\bos\.environ(?:\.get)?\[?\(?\s*['\"]([A-Z][A-Z0-9_]*)['\"]"
                ),
                "python-os-environ",
            ),
        )
        for item in self.inventory:
            relative = item["path"]
            path = self.source / relative
            if path.suffix.lower() not in TEXT_SUFFIXES or _is_example_env_file(
                path.name
            ):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            services = self._services_for_path(relative)
            if not services:
                services = {
                    service["key"]
                    for service in self.services
                    if service["role"] != "datastore"
                }
            for pattern, rule in patterns:
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    self._add_environment(
                        match.group(1),
                        "runtime",
                        services,
                        required=False,
                        path=relative,
                        rule=rule,
                        line=line,
                    )

    def _services_for_path(self, relative: str) -> set[str]:
        path = PurePosixPath(relative)
        matching: list[tuple[int, str]] = []
        for item in self.service_contexts:
            context = PurePosixPath(item.context)
            if item.context == "." or path == context or context in path.parents:
                matching.append(
                    (len(context.parts) if item.context != "." else 0, item.key)
                )
        if not matching:
            return set()
        deepest = max(depth for depth, _ in matching)
        return {key for depth, key in matching if depth == deepest}

    def _attach_environment_names(self) -> None:
        names_by_service: dict[str, set[str]] = {
            service["key"]: set() for service in self.services
        }
        for finding in self.environments.values():
            for service in finding.services:
                names_by_service.setdefault(service, set()).add(finding.name)
        for service in self.services:
            service["environmentNames"] = sorted(
                names_by_service.get(service["key"], set())
            )

    def _add_environment(
        self,
        name: str,
        scope: str,
        services: Iterable[str],
        required: bool,
        path: str,
        rule: str,
        *,
        line: int | None = None,
        force_sensitive: bool = False,
    ) -> None:
        if not ENV_NAME.fullmatch(name):
            return
        key = (scope, name)
        finding = self.environments.setdefault(
            key,
            EnvironmentFinding(
                name=name,
                scope=scope,
                sensitive=force_sensitive or _is_sensitive_name(name),
                public=_is_public_name(name) and not _is_sensitive_name(name),
            ),
        )
        finding.services.update(services)
        finding.required = finding.required or required
        finding.sensitive = (
            finding.sensitive or force_sensitive or _is_sensitive_name(name)
        )
        finding.public = _is_public_name(name) and not finding.sensitive
        evidence: dict[str, Any] = {"path": path, "rule": rule}
        if line is not None:
            evidence["line"] = line
        if evidence not in finding.evidence:
            finding.evidence.append(evidence)

    def _sorted_environments(self) -> list[EnvironmentFinding]:
        return [self.environments[key] for key in sorted(self.environments)]

    @staticmethod
    def _environment_payload(finding: EnvironmentFinding) -> dict[str, Any]:
        return {
            "name": finding.name,
            "scope": finding.scope,
            "services": sorted(finding.services),
            "required": finding.required,
            "sensitive": finding.sensitive,
            "public": finding.public,
            "configured": False,
        }

    @staticmethod
    def _evidence_environment_payload(finding: EnvironmentFinding) -> dict[str, Any]:
        return {
            "name": finding.name,
            "scope": finding.scope,
            "services": sorted(finding.services),
            "required": finding.required,
            "sensitive": finding.sensitive,
            "public": finding.public,
            "evidence": sorted(
                finding.evidence,
                key=lambda item: (item["path"], item.get("line", 0), item["rule"]),
            ),
        }

    def _read_source_text(self, relative: str) -> str:
        path = self.source / relative
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            self._warning("SOURCE_TEXT_UNREADABLE", relative)
            return ""

    def _warning(self, code: str, path: str) -> None:
        self.warnings.add(f"{code}:{path}")

    def _block(self, code: str, path: str, field: str) -> None:
        self.blockers.add(f"{code}:{path}#{field}")
        self.evidence.append(
            {"path": path, "rule": "policy-blocker", "name": code, "field": field}
        )

    def _block_unknown_fields(
        self,
        value: Mapping[str, Any],
        allowed: set[str],
        path: str,
        prefix: str,
    ) -> None:
        for field_name in sorted(set(value) - allowed, key=str):
            if isinstance(field_name, str) and field_name.startswith("x-"):
                continue
            self._block("COMPOSE_FIELD_UNSUPPORTED", path, f"{prefix}.{field_name}")


def analyze_source(
    source: Path | str, metadata: Mapping[str, Any], output_dir: Path | str
) -> dict[str, Any]:
    source_path = Path(source).expanduser().resolve(strict=True)
    if not source_path.is_dir():
        raise AnalysisError("LAE_SOURCE_INVALID", "Source must be a directory")
    output_path = Path(output_dir).expanduser().resolve(strict=False)
    if _is_relative_to(output_path, source_path):
        raise AnalysisError(
            "LAE_OUTPUT_INVALID",
            "Output directory must not be inside the source directory",
        )
    analyzer = Analyzer(source_path, metadata)
    evidence, deployment_plan, build_plan_proposal = analyzer.run()
    ai_required = os.environ.get("LAE_AGENT_AI_REQUIRED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    diagnostic_status = "diagnostic_failed"
    diagnostic_mode = "deterministic_fallback"
    diagnostic_code = "AI_ANALYSIS_NOT_CONFIGURED"
    provider_model: str | None = None
    knowledge_version = KNOWLEDGE_VERSION
    try:
        ai_config = AIControllerClientConfig.from_env()
        if ai_config is not None:
            ai_response = request_ai_analysis(
                ai_config,
                build_ai_request(
                    source_path,
                    deployment_plan,
                    build_plan_proposal,
                    evidence,
                ),
            )
            deployment_plan, manifest_candidate = apply_ai_proposal(
                ai_response["proposal"], deployment_plan, build_plan_proposal
            )
            diagnostic_status = "succeeded"
            diagnostic_mode = "ai"
            diagnostic_code = "AI_ANALYSIS_SUCCEEDED"
            raw_model = ai_response.get("model")
            provider_model = raw_model if isinstance(raw_model, str) else None
            knowledge_version = ai_response["knowledgeVersion"]
        else:
            manifest_candidate = manifest_candidate_from_plan(deployment_plan)
    except AIDiagnosticError as exc:
        diagnostic_code = exc.code
        manifest_candidate = manifest_candidate_from_plan(deployment_plan)

    if diagnostic_status == "diagnostic_failed":
        deployment_plan["warnings"] = sorted(
            {*deployment_plan["warnings"], "AI_ANALYSIS_DEGRADED_DETERMINISTIC_FALLBACK"}
        )
    _validate_plan("deployment-plan.v1.schema.json", deployment_plan)
    _validate_plan_semantics(deployment_plan, build_plan_proposal)
    verdict = (
        "unsupported"
        if deployment_plan["policy"]["decision"] == "deny"
        else (
            "diagnostic_failed"
            if ai_required and diagnostic_status == "diagnostic_failed"
            else _verdict(deployment_plan["policy"]["decision"])
        )
    )
    evidence["ai"] = {
        "status": diagnostic_status,
        "mode": diagnostic_mode,
        "code": diagnostic_code,
        "model": provider_model,
        "manifestCandidate": manifest_candidate,
        "knowledgeVersion": knowledge_version,
    }
    evidence["verdict"] = verdict
    evidence["unsupported"] = (
        unsupported_findings(evidence) if verdict == "unsupported" else []
    )
    evidence["warnings"] = deployment_plan["warnings"]
    evidence["blockers"] = deployment_plan["blockers"]
    output_path.mkdir(parents=True, exist_ok=True)

    artifact_values = (
        (
            "evidence",
            "evidence.json",
            "application/vnd.lae.evidence+json",
            evidence,
        ),
        (
            "deploymentPlan",
            "deployment-plan.json",
            "application/vnd.lae.deployment-plan+json",
            deployment_plan,
        ),
        (
            "buildPlan",
            "build-plan-proposal.json",
            "application/vnd.lae.build-plan-proposal+json",
            build_plan_proposal,
        ),
    )
    artifacts: dict[str, Any] = {}
    for key, filename, media_type, value in artifact_values:
        digest = atomic_write_json(output_path / filename, value)
        artifacts[key] = {
            "path": filename,
            "digest": digest,
            "mediaType": media_type,
            "sizeBytes": len(canonical_bytes(value)),
        }
    result = {
        "schemaVersion": "lae.agent-analysis-result/v1",
        "externalOperationId": analyzer.metadata["externalOperationId"],
        "tenantRef": analyzer.metadata["tenantRef"],
        "applicationRef": analyzer.metadata["applicationRef"],
        "resolvedCommit": analyzer.metadata["resolvedCommit"],
        "sourceSnapshotId": analyzer.metadata["sourceSnapshotId"],
        "sourceSnapshotDigest": analyzer.metadata["sourceSnapshotDigest"],
        "policyVersion": analyzer.metadata["policyVersion"],
        "status": "succeeded",
        "decision": deployment_plan["policy"]["decision"],
        "verdict": verdict,
        "diagnosticStatus": diagnostic_status,
        "diagnosticMode": diagnostic_mode,
        "diagnosticCode": diagnostic_code,
        "knowledgeVersion": knowledge_version,
        "blockers": evidence["unsupported"],
        "artifacts": artifacts,
    }
    atomic_write_json(output_path / "result.json", result)
    return result


def _verdict(decision: str) -> str:
    return {
        "allow": "deployable",
        "needs_configuration": "needs_input",
        "deny": "unsupported",
    }[decision]


def _validate_metadata(metadata: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(metadata, Mapping):
        raise AnalysisError("LAE_METADATA_INVALID", "Metadata must be a JSON object")
    for key in metadata:
        if not isinstance(key, str) or FORBIDDEN_METADATA_KEY.search(key):
            raise AnalysisError(
                "LAE_METADATA_FORBIDDEN", "Metadata contains a forbidden field"
            )
    required = (
        "externalOperationId",
        "tenantRef",
        "applicationRef",
        "resolvedCommit",
        "sourceSnapshotId",
        "sourceSnapshotDigest",
        "policyVersion",
    )
    result: dict[str, str] = {}
    for key in required:
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            raise AnalysisError(
                "LAE_METADATA_INVALID",
                f"Metadata field {key} must be a non-empty string",
            )
        result[key] = value.strip()
        if not REFERENCE.fullmatch(result[key]):
            raise AnalysisError(
                "LAE_METADATA_INVALID",
                f"Metadata field {key} contains unsupported characters",
            )
    if not COMMIT.fullmatch(result["resolvedCommit"]):
        raise AnalysisError(
            "LAE_METADATA_INVALID",
            "resolvedCommit must be a full lowercase Git object id",
        )
    if not DIGEST.fullmatch(result["sourceSnapshotDigest"]):
        raise AnalysisError(
            "LAE_METADATA_INVALID", "sourceSnapshotDigest must be a sha256 digest"
        )
    return result


def _validate_plan(schema: str, value: Mapping[str, Any]) -> None:
    issues = validate_instance(schema, value)
    if issues:
        raise AnalysisError(
            "LAE_PLAN_INVALID",
            f"Generated {schema} failed contract validation at {issues[0].path}",
        )


def _validate_plan_semantics(
    deployment_plan: Mapping[str, Any], build_plan: Mapping[str, Any]
) -> None:
    """Validate cross-references that JSON Schema cannot currently express."""

    services = deployment_plan["services"]
    service_keys = [item["key"] for item in services]
    builds = build_plan["builds"]
    build_keys = [item["key"] for item in builds]
    external_images = build_plan["externalImages"]
    external_keys = [item["key"] for item in external_images]
    image_keys = [*build_keys, *external_keys]
    if len(service_keys) != len(set(service_keys)) or len(image_keys) != len(
        set(image_keys)
    ):
        raise AnalysisError(
            "LAE_PLAN_SEMANTICS_INVALID", "Generated plan contains duplicate keys"
        )
    service_key_set = set(service_keys)
    build_key_set = set(build_keys)
    external_by_key = {item["key"]: item for item in external_images}
    if not set(external_by_key) <= service_key_set:
        raise AnalysisError(
            "LAE_PLAN_SEMANTICS_INVALID",
            "Generated external image references an unknown service",
        )
    service_by_key = {item["key"]: item for item in services}

    for service in services:
        dependencies = set(service["dependencies"])
        if service["key"] in dependencies or not dependencies <= service_key_set:
            raise AnalysisError(
                "LAE_PLAN_SEMANTICS_INVALID",
                "Generated service dependencies are invalid",
            )
        image = service["image"]
        if image["source"] == "build":
            if (
                set(image) != {"source", "buildKey"}
                or image.get("buildKey") not in build_key_set
            ):
                raise AnalysisError(
                    "LAE_PLAN_SEMANTICS_INVALID",
                    "Generated build image reference is invalid",
                )
        else:
            external = external_by_key.get(service["key"])
            if set(image) != {"source", "ref"} or (
                is_safe_external_image_reference(image.get("ref"))
                and (
                    external is None
                    or external["ref"] != image["ref"]
                    or external["platform"] != "linux/amd64"
                )
            ):
                raise AnalysisError(
                    "LAE_PLAN_SEMANTICS_INVALID",
                    "Generated external image reference is invalid",
                )

    primary_count = sum(bool(route["primary"]) for route in deployment_plan["routes"])
    if deployment_plan["routes"] and primary_count != 1:
        raise AnalysisError(
            "LAE_PLAN_SEMANTICS_INVALID",
            "Generated routes must contain exactly one primary",
        )
    for route in deployment_plan["routes"]:
        service = service_by_key.get(route["serviceKey"])
        if (
            service is None
            or service.get("role") != "http"
            or service.get("port") != route["containerPort"]
        ):
            raise AnalysisError(
                "LAE_PLAN_SEMANTICS_INVALID",
                "Generated route does not match its service",
            )
    for volume in deployment_plan["volumes"]:
        if not set(volume["serviceKeys"]) <= service_key_set:
            raise AnalysisError(
                "LAE_PLAN_SEMANTICS_INVALID",
                "Generated volume references an unknown service",
            )
    for environment in deployment_plan["environment"]:
        if not set(environment["services"]) <= service_key_set:
            raise AnalysisError(
                "LAE_PLAN_SEMANTICS_INVALID",
                "Generated environment references an unknown service",
            )
    graph: dict[str, set[str]] = {}
    for build in builds:
        dependencies = set(build["dependsOnBuilds"])
        if build["key"] in dependencies or not dependencies <= build_key_set:
            raise AnalysisError(
                "LAE_PLAN_SEMANTICS_INVALID", "Generated build dependencies are invalid"
            )
        graph[build["key"]] = dependencies
    _assert_acyclic(graph)


def _assert_acyclic(graph: Mapping[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise AnalysisError(
                "LAE_PLAN_SEMANTICS_INVALID",
                "Generated build dependency graph contains a cycle",
            )
        if key in visited:
            return
        visiting.add(key)
        for dependency in graph.get(key, set()):
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in sorted(graph):
        visit(key)


def _stable_identifier(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _unique_key(value: str, used: set[str]) -> str:
    normalized = _base_key(value)
    candidate = normalized
    if candidate in used:
        suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        candidate = f"{normalized}-{suffix}"
    used.add(candidate)
    return candidate


def _base_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    if not normalized or not normalized[0].isalpha():
        normalized = f"service-{normalized}".strip("-")
    return normalized


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_example_env_file(name: str) -> bool:
    lowered = name.lower()
    if not lowered.startswith(".env"):
        return False
    return any(
        marker in lowered.split(".")
        for marker in ("dist", "example", "sample", "template")
    )


def _is_private_env_file(name: str) -> bool:
    return name.lower().startswith(".env") and not _is_example_env_file(name)


def _is_sensitive_name(name: str) -> bool:
    return any(marker in name for marker in SENSITIVE_MARKERS)


def _is_public_name(name: str) -> bool:
    return name.startswith(PUBLIC_PREFIXES)


def _compose_labels(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {str(key): str(item) for key, item in value.items() if item is not None}
    result: dict[str, str] = {}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            if isinstance(item, str):
                key, separator, raw_value = item.partition("=")
                if separator:
                    result[key] = raw_value
    return result


def _classify_compose_role(
    name: str, value: Mapping[str, Any], labels: Mapping[str, str]
) -> str:
    requested = labels.get("lae.role")
    if requested in {"http", "worker", "internal", "datastore"}:
        return requested
    image = str(value.get("image", "")).lower()
    tokens = set(re.split(r"[^a-z0-9]+", name.lower())) | set(
        re.split(r"[^a-z0-9]+", image)
    )
    if tokens & DATASTORE_MARKERS:
        return "datastore"
    if tokens & WORKER_MARKERS:
        return "worker"
    if tokens & HTTP_MARKERS or tokens & {"caddy", "httpd", "nginx", "traefik"}:
        return "http"
    return "internal"


def _compose_dependency_names(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        names = value.keys()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        names = value
    else:
        names = []
    return {name for name in names if isinstance(name, str)}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return [value]


def _container_port(value: Any) -> tuple[int, str] | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return (value, "tcp") if 1 <= value <= 65535 else None
    if isinstance(value, str):
        raw_port, separator, protocol = value.partition("/")
        if raw_port.isdigit() and 1 <= int(raw_port) <= 65535:
            return int(raw_port), protocol.lower() if separator else "tcp"
    return None


def _published_container_port(value: Any) -> tuple[int, str] | None:
    if isinstance(value, Mapping):
        target = value.get("target")
        protocol = str(value.get("protocol", "tcp")).lower()
        if isinstance(target, int) and 1 <= target <= 65535:
            return target, protocol
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return _container_port(value)
    if not isinstance(value, str):
        return None
    raw, separator, protocol = value.partition("/")
    segments = raw.rsplit(":", 2)
    target = segments[-1]
    if target.isdigit() and 1 <= int(target) <= 65535:
        return int(target), protocol.lower() if separator else "tcp"
    return None


def _port_has_explicit_host_publish(value: Any) -> bool:
    if isinstance(value, Mapping):
        return (
            any(
                value.get(field) not in (None, "") for field in ("published", "host_ip")
            )
            or value.get("mode") == "host"
        )
    if isinstance(value, int) and not isinstance(value, bool):
        return False
    if not isinstance(value, str):
        return False
    raw = value.partition("/")[0]
    return ":" in raw


def _select_http_port(ports: Iterable[int]) -> int | None:
    values = sorted(set(ports), key=lambda item: (item not in HTTP_PORTS, item))
    return values[0] if values else None


def _default_port_for_service(
    value: Mapping[str, Any], image: Mapping[str, Any]
) -> int:
    ref = str(image.get("ref", "")).lower()
    if "nginx" in ref or "caddy" in ref or "httpd" in ref:
        return 80
    if value.get("build"):
        return 3000
    return 8080


def _compose_resources(value: Mapping[str, Any]) -> tuple[str, int]:
    cpu_value: Any = value.get("cpus")
    memory_value: Any = value.get("mem_limit")
    deploy = value.get("deploy")
    if isinstance(deploy, Mapping):
        resources = deploy.get("resources")
        if isinstance(resources, Mapping):
            limits = resources.get("limits")
            if isinstance(limits, Mapping):
                cpu_value = limits.get("cpus", cpu_value)
                memory_value = limits.get("memory", memory_value)
    cpu = _normalize_cpu(cpu_value)
    memory = _parse_memory_mib(memory_value)
    return cpu, max(64, memory)


def _normalize_cpu(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0.50"
    if number <= 0:
        return "0.50"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _parse_memory_mib(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(1, (value + 1024 * 1024 - 1) // (1024 * 1024))
    if not isinstance(value, str):
        return DEFAULT_MEMORY_MIB
    match = re.fullmatch(
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?i?b?)?\s*", value, re.IGNORECASE
    )
    if not match:
        return DEFAULT_MEMORY_MIB
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    factors = {
        "b": 1 / (1024 * 1024),
        "k": 1000 / (1024 * 1024),
        "kb": 1000 / (1024 * 1024),
        "ki": 1 / 1024,
        "kib": 1 / 1024,
        "m": 1_000_000 / (1024 * 1024),
        "mb": 1_000_000 / (1024 * 1024),
        "mi": 1,
        "mib": 1,
        "g": 1_000_000_000 / (1024 * 1024),
        "gb": 1_000_000_000 / (1024 * 1024),
        "gi": 1024,
        "gib": 1024,
        "t": 1_000_000_000_000 / (1024 * 1024),
        "tb": 1_000_000_000_000 / (1024 * 1024),
        "ti": 1024 * 1024,
        "tib": 1024 * 1024,
    }
    return max(1, int(number * factors.get(unit, factors["b"])))


def _compose_volume_parts(value: Any) -> tuple[str | None, str | None, str | None]:
    if isinstance(value, Mapping):
        source = value.get("source")
        target = value.get("target")
        volume_type = value.get("type")
        return (
            source if isinstance(source, str) else None,
            target if isinstance(target, str) else None,
            volume_type if isinstance(volume_type, str) else None,
        )
    if not isinstance(value, str):
        return None, None, None
    windows_match = re.match(r"^([A-Za-z]:[\\/][^:]*):(.+)$", value)
    if windows_match:
        return windows_match.group(1), windows_match.group(2).split(":", 1)[0], "bind"
    segments = value.split(":")
    if len(segments) == 1:
        return None, segments[0], "volume"
    source, target = segments[0], segments[1]
    return source, target, "bind" if _looks_like_host_path(source) else "volume"


def _contains_docker_socket(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            "containerd.sock",
            "docker.sock",
            "podman.sock",
            "/run/containerd",
            "/var/run/crio",
        )
    )


def _looks_like_host_path(value: str) -> bool:
    return (
        value.startswith(("/", "./", "../", "~", "${"))
        or re.match(r"^[A-Za-z]:[\\/]", value) is not None
    )


def _normalize_source_path(value: str) -> str | None:
    if (
        not value
        or "://" in value
        or value.startswith(("/", "~", "${"))
        or re.match(r"^[A-Za-z]:", value)
    ):
        return None
    pure = PurePosixPath(value.replace("\\", "/"))
    if ".." in pure.parts:
        return None
    normalized = pure.as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "."


def _is_safe_image_reference(value: str) -> bool:
    return is_safe_external_image_reference(value)


def _is_safe_compose_build_image_reference(value: str) -> bool:
    """Validate a local logical tag without treating it as an external pull.

    The external-image policy intentionally rejects ``latest`` and untagged
    references.  A Compose build owner may still use either form as a local
    tag, so normalize only for syntax validation while keeping the original
    exact string as the consumer-to-build mapping key.
    """

    if not value or "@" in value:
        return False
    last_slash = value.rfind("/")
    last_component = value[last_slash + 1 :]
    if ":" in last_component:
        repository, tag = last_component.rsplit(":", 1)
        if not repository or not tag:
            return False
        safe_tag = "local" if tag.lower() == "latest" else tag
        candidate = value[: last_slash + 1] + repository + ":" + safe_tag
    else:
        candidate = value + ":local"
    return is_safe_external_image_reference(candidate)


def _is_safe_source_entry(root: Path, relative: str, *, directory: bool) -> bool:
    candidate = root
    if relative != ".":
        for part in PurePosixPath(relative).parts:
            candidate = candidate / part
            try:
                mode = candidate.lstat().st_mode
            except OSError:
                return False
            if stat.S_ISLNK(mode):
                return False
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return False
    if not _is_relative_to(resolved, root):
        return False
    return resolved.is_dir() if directory else resolved.is_file()


def _join_source_path(context: str, dockerfile: str) -> str | None:
    normalized_dockerfile = _normalize_source_path(dockerfile)
    if normalized_dockerfile is None:
        return None
    if context == ".":
        return normalized_dockerfile
    joined = PurePosixPath(context) / normalized_dockerfile
    if ".." in joined.parts:
        return None
    return joined.as_posix()


def _is_safe_source_path(value: str) -> bool:
    return _normalize_source_path(value) is not None


def _name_list(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return [str(key) for key in value]
    result: list[str] = []
    for item in _as_list(value):
        if isinstance(item, str):
            result.append(item.partition("=")[0])
    return result


def _build_secret_names(value: Any) -> list[str]:
    result: list[str] = []
    for item in _as_list(value):
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, Mapping):
            candidate = item.get("target") or item.get("source")
            if isinstance(candidate, str):
                result.append(candidate)
    return result


def _dockerfile_ports(value: str) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for line in value.splitlines():
        match = re.match(r"\s*EXPOSE\s+(.+?)\s*(?:#.*)?$", line, re.IGNORECASE)
        if not match:
            continue
        for item in match.group(1).split():
            parsed = _container_port(item)
            if parsed is not None:
                result.append(parsed)
    return result


def _dockerfile_arg_names(value: str) -> set[str]:
    return {
        match.group(1)
        for match in re.finditer(r"(?im)^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)", value)
    }


def _dockerfile_secret_names(value: str) -> set[str]:
    return {
        match.group(1)
        for match in re.finditer(
            r"--mount=type=secret(?:,[^\s]*)?,id=([A-Za-z_][A-Za-z0-9_]*)", value
        )
    }


def _node_default_port(path: Path) -> int:
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 3000
    dependencies: set[str] = set()
    if isinstance(package, Mapping):
        for section in ("dependencies", "devDependencies"):
            raw = package.get(section)
            if isinstance(raw, Mapping):
                dependencies.update(str(name) for name in raw)
    if {"vite", "astro"} & dependencies:
        return 4173
    return 3000
