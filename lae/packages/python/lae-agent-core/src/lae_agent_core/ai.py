from __future__ import annotations

import copy
import hashlib
import hmac
import ipaddress
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from lae_contracts import validate_instance

from .canonical import canonical_bytes

AI_REQUEST_SCHEMA = "lae.ai-analysis-request/v1"
AI_RESPONSE_SCHEMA = "lae.ai-analysis-response/v1"
MANIFEST_CANDIDATE_SCHEMA = "lae.luma-manifest-candidate/v1"
KNOWLEDGE_VERSION = "2026-07-14.1"
MAX_AI_REQUEST_BYTES = 256 * 1024
MAX_AI_RESPONSE_BYTES = 1024 * 1024
MAX_PROMPT_FILES = 16

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,511}$")
_SECRET_NAME = re.compile(
    r"(?:access[_-]?key|api[_-]?key|auth|authorization|credential|database[_-]?url|password|private[_-]?key|secret|token)",
    re.IGNORECASE,
)
_PRIVATE_ENV = re.compile(r"^\.env(?:\..+)?$")
_PROMPT_FILENAMES = {
    "Dockerfile",
    "Procfile",
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "README",
    "README.md",
}
_PROMPT_SUFFIXES = {".dockerfile", ".json", ".toml", ".yaml", ".yml"}
_ENTRYPOINT_FILENAMES = {
    "app.py",
    "main.py",
    "server.py",
    "app.js",
    "app.ts",
    "index.js",
    "index.ts",
    "main.js",
    "main.ts",
    "server.js",
    "server.ts",
}


class AIDiagnosticError(RuntimeError):
    """A provider/controller diagnostic failed without classifying user code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect blocked", headers, fp)


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass(frozen=True, slots=True)
class AIControllerClientConfig:
    url: str
    token: str
    timeout_seconds: float = 45.0

    @classmethod
    def from_env(
        cls, values: Mapping[str, str] | None = None
    ) -> "AIControllerClientConfig | None":
        env = os.environ if values is None else values
        url = env.get("LAE_AGENT_CONTROLLER_URL", "").strip().rstrip("/")
        token = env.get("LAE_AGENT_CONTROLLER_TOKEN", "").strip()
        if not url and not token:
            return None
        if not url or not token or not _safe_configured_url(url, allow_loopback=True):
            raise AIDiagnosticError("AI_CONTROLLER_CONFIGURATION_INVALID")
        try:
            timeout = float(env.get("LAE_AGENT_CONTROLLER_TIMEOUT_SECONDS", "45"))
        except ValueError as exc:
            raise AIDiagnosticError("AI_CONTROLLER_CONFIGURATION_INVALID") from exc
        if not 1 <= timeout <= 120:
            raise AIDiagnosticError("AI_CONTROLLER_CONFIGURATION_INVALID")
        return cls(url=url, token=token, timeout_seconds=timeout)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 40.0

    @classmethod
    def from_env(
        cls, values: Mapping[str, str] | None = None
    ) -> "OpenAICompatibleConfig | None":
        env = os.environ if values is None else values
        base_url = env.get(
            "LAE_AGENT_LLM_BASE_URL", env.get("LAE_AGENT_AI_BASE_URL", "")
        ).strip().rstrip("/")
        api_key = env.get(
            "LAE_AGENT_LLM_API_KEY", env.get("LAE_AGENT_AI_API_KEY", "")
        ).strip()
        model = env.get(
            "LAE_AGENT_LLM_MODEL", env.get("LAE_AGENT_AI_MODEL", "")
        ).strip()
        if not base_url and not api_key and not model:
            return None
        if (
            not base_url
            or not api_key
            or not model
            or not _safe_configured_url(base_url, allow_loopback=True)
        ):
            raise AIDiagnosticError("AI_PROVIDER_CONFIGURATION_INVALID")
        try:
            timeout = float(
                env.get(
                    "LAE_AGENT_LLM_TIMEOUT_SECONDS",
                    env.get("LAE_AGENT_AI_TIMEOUT_SECONDS", "40"),
                )
            )
        except ValueError as exc:
            raise AIDiagnosticError("AI_PROVIDER_CONFIGURATION_INVALID") from exc
        if not 1 <= timeout <= 120:
            raise AIDiagnosticError("AI_PROVIDER_CONFIGURATION_INVALID")
        return cls(base_url, api_key, model, timeout)


def request_ai_analysis(
    config: AIControllerClientConfig, request: Mapping[str, Any]
) -> dict[str, Any]:
    raw = canonical_bytes(request)
    if len(raw) > MAX_AI_REQUEST_BYTES:
        raise AIDiagnosticError("AI_REQUEST_TOO_LARGE")
    http_request = urllib.request.Request(
        f"{config.url}/v1/analyze",
        data=raw,
        headers={
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    body: bytes | None = None
    last_error: BaseException | None = None
    for attempt in range(3):
        try:
            with _NO_REDIRECT_OPENER.open(
                http_request, timeout=config.timeout_seconds
            ) as response:
                body = response.read(MAX_AI_RESPONSE_BYTES + 1)
            break
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 502, 503, 504} or attempt == 2:
                break
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt == 2:
                break
        time.sleep(0.5 * (2**attempt))
    if body is None:
        raise AIDiagnosticError("AI_CONTROLLER_UNAVAILABLE") from last_error
    if len(body) > MAX_AI_RESPONSE_BYTES:
        raise AIDiagnosticError("AI_RESPONSE_TOO_LARGE")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AIDiagnosticError("AI_RESPONSE_INVALID") from exc
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != AI_RESPONSE_SCHEMA
        or value.get("status") != "succeeded"
        or not isinstance(value.get("proposal"), dict)
        or not isinstance(value.get("knowledgeVersion"), str)
        or value.get("knowledgeVersion") != KNOWLEDGE_VERSION
    ):
        raise AIDiagnosticError("AI_RESPONSE_INVALID")
    return value


def call_openai_compatible(
    config: OpenAICompatibleConfig,
    request: Mapping[str, Any],
    knowledge_pack: Mapping[str, Any],
) -> dict[str, Any]:
    prompt = _provider_prompt(request, knowledge_pack)
    body = canonical_bytes(
        {
            "model": config.model,
            "max_tokens": 3000,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are LAE's deployment diagnostic planner. Return only one JSON "
                        "object. Never invent secret values, host ports, TCP/UDP public routes, "
                        "privileged containers, host mounts, custom domains, or infrastructure "
                        "addresses. Preserve every deterministic blocker and warning."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
    )
    http_request = urllib.request.Request(
        f"{config.base_url}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with _NO_REDIRECT_OPENER.open(http_request, timeout=config.timeout_seconds) as response:
            raw = response.read(MAX_AI_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise AIDiagnosticError("AI_PROVIDER_UNAVAILABLE") from exc
    if len(raw) > MAX_AI_RESPONSE_BYTES:
        raise AIDiagnosticError("AI_PROVIDER_RESPONSE_TOO_LARGE")
    try:
        response = json.loads(raw)
        choices = response["choices"]
        content = choices[0]["message"]["content"]
        suggestions = json.loads(content)
    except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AIDiagnosticError("AI_PROVIDER_RESPONSE_INVALID") from exc
    if not isinstance(suggestions, dict):
        raise AIDiagnosticError("AI_PROVIDER_RESPONSE_INVALID")
    return _proposal_from_suggestions(request, suggestions)


def build_ai_request(
    source: Path,
    deployment_plan: Mapping[str, Any],
    build_plan: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    files: list[dict[str, str]] = []
    inventory_paths = {
        item.get("path")
        for item in evidence.get("inventory", [])
        if isinstance(item, Mapping) and isinstance(item.get("path"), str)
    }
    for relative in sorted(inventory_paths, key=_prompt_priority):
        if len(files) >= MAX_PROMPT_FILES or not _include_prompt_file(relative):
            continue
        # Raw tenant source is deliberately not sent to an external model.
        # The deterministic analyzer has already converted it into bounded
        # topology/build/environment signals below; file names are enough to
        # identify framework conventions without disclosing content.
        files.append({"path": relative})
    request = {
        "schemaVersion": AI_REQUEST_SCHEMA,
        "source": {
            "digest": deployment_plan["sourceDigest"],
            "kind": deployment_plan["kind"],
            "files": files,
        },
        "deterministic": {
            "deploymentPlan": deployment_plan,
            "buildPlan": build_plan,
            "findings": evidence.get("findings", []),
        },
        "expectedOutput": {
            "deploymentPlan": "lae.deployment-plan/v1",
            "manifestCandidate": MANIFEST_CANDIDATE_SCHEMA,
            "knowledgeVersion": KNOWLEDGE_VERSION,
        },
    }
    if len(canonical_bytes(request)) > MAX_AI_REQUEST_BYTES:
        raise AIDiagnosticError("AI_REQUEST_TOO_LARGE")
    return request


def apply_ai_proposal(
    proposal: Mapping[str, Any],
    baseline: Mapping[str, Any],
    build_plan: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate = proposal.get("deploymentPlan")
    manifest = proposal.get("manifestCandidate")
    if not isinstance(candidate, Mapping) or not isinstance(manifest, Mapping):
        raise AIDiagnosticError("AI_PROPOSAL_INVALID")
    candidate = copy.deepcopy(dict(candidate))
    if validate_instance("deployment-plan.v1.schema.json", candidate):
        raise AIDiagnosticError("AI_PROPOSAL_INVALID")

    for field in ("schemaVersion", "sourceRevisionId", "sourceDigest", "kind"):
        if candidate.get(field) != baseline.get(field):
            raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")
    baseline_services = {item["key"]: item for item in baseline["services"]}
    candidate_services = {item["key"]: item for item in candidate["services"]}
    if set(candidate_services) != set(baseline_services):
        raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")
    for key, service in baseline_services.items():
        proposed = candidate_services[key]
        for field in set(service) | set(proposed):
            if field != "environmentNames" and proposed.get(field) != service.get(field):
                raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")
        if set(service["environmentNames"]) != set(proposed["environmentNames"]):
            raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")

    if candidate["routes"] != baseline["routes"] or candidate["volumes"] != baseline["volumes"]:
        raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")

    if set(baseline["blockers"]) != set(candidate["blockers"]):
        raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")
    if not set(baseline["warnings"]) <= set(candidate["warnings"]):
        raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")
    _assert_environment_not_weakened(
        baseline["environment"],
        candidate["environment"],
        service_keys=set(candidate_services),
        service_environment={
            key: set(item["environmentNames"])
            for key, item in candidate_services.items()
        },
    )

    decision = (
        "deny"
        if candidate["blockers"]
        else (
            "needs_configuration"
            if any(item["required"] for item in candidate["environment"])
            else "allow"
        )
    )
    candidate["policy"] = {"version": baseline["policy"]["version"], "decision": decision}
    candidate["warnings"] = sorted(set(candidate["warnings"]))
    candidate["blockers"] = sorted(set(candidate["blockers"]))
    candidate["environment"] = sorted(
        candidate["environment"], key=lambda item: (item["name"], item["scope"])
    )
    candidate["planId"] = "plan_" + hashlib.sha256(
        canonical_bytes({key: value for key, value in candidate.items() if key != "planId"})
    ).hexdigest()[:24]
    if validate_instance("deployment-plan.v1.schema.json", candidate):
        raise AIDiagnosticError("AI_PROPOSAL_INVALID")
    _validate_plan_relationships(candidate, build_plan)

    normalized_manifest = _normalize_manifest_candidate(manifest)
    if normalized_manifest != manifest_candidate_from_plan(candidate):
        raise AIDiagnosticError("AI_MANIFEST_CANDIDATE_INCONSISTENT")
    return candidate, normalized_manifest


def manifest_candidate_from_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    routes = {item["serviceKey"]: item for item in plan["routes"]}
    services = []
    for service in sorted(plan["services"], key=lambda item: item["key"]):
        item: dict[str, Any] = {
            "key": service["key"],
            "role": service["role"],
            "image": service["image"],
            "dependencies": service["dependencies"],
            "environmentNames": service["environmentNames"],
            "publicHttp": service["key"] in routes,
        }
        if "port" in service:
            item["port"] = service["port"]
        services.append(item)
    return {
        "schemaVersion": MANIFEST_CANDIDATE_SCHEMA,
        "kind": plan["kind"],
        "region": "cn",
        "services": services,
        "volumes": plan["volumes"],
        "domainAssignment": "platform-random-itool-tech",
    }


def unsupported_findings(evidence: Mapping[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for item in evidence.get("findings", []):
        if not isinstance(item, Mapping) or item.get("rule") != "policy-blocker":
            continue
        code = item.get("name")
        path = item.get("path")
        field = item.get("field")
        if not all(isinstance(value, str) for value in (code, path, field)):
            continue
        findings.append(
            {
                "code": code,
                "path": path,
                "field": field,
                "remediation": _remediation(code),
            }
        )
    return sorted(findings, key=lambda item: (item["code"], item["path"], item["field"]))


def authorized(token: str, header: str | None) -> bool:
    if not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(token.encode(), header[7:].strip().encode())


def _safe_configured_url(value: str, *, allow_loopback: bool) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        return False
    if parsed.scheme == "http" and not (
        allow_loopback and parsed.hostname in {"127.0.0.1", "::1"}
    ):
        return False
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return parsed.hostname.lower() != "localhost"
    return bool(allow_loopback and address.is_loopback) or not (
        address.is_private
        or address.is_link_local
        or address.is_loopback
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def load_knowledge_pack(path: Path | str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AIDiagnosticError("AI_KNOWLEDGE_UNAVAILABLE") from exc
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != "lae.knowledge-pack/v1"
        or value.get("knowledgeVersion") != KNOWLEDGE_VERSION
        or not all(
            key in value
            for key in (
                "product",
                "manifest",
                "security",
                "resourcesAndPlacement",
                "environment",
                "healthAndFrameworkRecipes",
                "verdicts",
                "blockers",
            )
        )
    ):
        raise AIDiagnosticError("AI_KNOWLEDGE_INVALID")
    return value


def _provider_prompt(
    request: Mapping[str, Any], knowledge_pack: Mapping[str, Any]
) -> str:
    return (
        "Review the closed structured deployment summary below. Return only "
        "{environmentUpdates:[{name,scope,required,sensitive}]}. Updates may refer "
        "only to listed environment variables and may only promote required or "
        "sensitive from false to true. Do not return source, paths, values, services, "
        "routes, manifests, explanations, or any other fields.\n"
        + "\nKNOWLEDGE PACK:\n"
        + canonical_bytes(knowledge_pack).decode("utf-8")
        + "\nSTRUCTURED SUMMARY:\n"
        + canonical_bytes(_provider_summary(request)).decode("utf-8")
    )


def _provider_summary(request: Mapping[str, Any]) -> dict[str, Any]:
    plan = request["deterministic"]["deploymentPlan"]
    return {
        "kind": plan["kind"],
        "services": [
            {
                "key": item["key"],
                "role": item["role"],
                "port": item.get("port"),
                "imageSource": item["image"]["source"],
                "environmentNames": item["environmentNames"],
            }
            for item in plan["services"]
        ],
        "publicHttpPorts": [item["containerPort"] for item in plan["routes"]],
        "managedVolumeCount": len(plan["volumes"]),
        "environment": [
            {
                "name": item["name"],
                "scope": item["scope"],
                "required": item["required"],
                "sensitive": item["sensitive"],
                "public": item["public"],
            }
            for item in plan["environment"]
        ],
        "deterministicBlockerCodes": sorted(
            {
                str(item.get("name"))
                for item in request["deterministic"]["findings"]
                if item.get("rule") == "policy-blocker"
            }
        ),
    }


def _proposal_from_suggestions(
    request: Mapping[str, Any], suggestions: Mapping[str, Any]
) -> dict[str, Any]:
    if set(suggestions) != {"environmentUpdates"} or not isinstance(
        suggestions["environmentUpdates"], list
    ):
        raise AIDiagnosticError("AI_PROVIDER_RESPONSE_INVALID")
    if len(suggestions["environmentUpdates"]) > 128:
        raise AIDiagnosticError("AI_PROVIDER_RESPONSE_INVALID")
    plan = copy.deepcopy(request["deterministic"]["deploymentPlan"])
    by_key = {(item["name"], item["scope"]): item for item in plan["environment"]}
    seen: set[tuple[str, str]] = set()
    for update in suggestions["environmentUpdates"]:
        if not isinstance(update, Mapping) or set(update) != {
            "name",
            "scope",
            "required",
            "sensitive",
        }:
            raise AIDiagnosticError("AI_PROVIDER_RESPONSE_INVALID")
        if not isinstance(update["name"], str) or not isinstance(update["scope"], str):
            raise AIDiagnosticError("AI_PROVIDER_RESPONSE_INVALID")
        key = (update["name"], update["scope"])
        if (
            key in seen
            or key not in by_key
            or not isinstance(update["required"], bool)
            or not isinstance(update["sensitive"], bool)
        ):
            raise AIDiagnosticError("AI_PROVIDER_RESPONSE_INVALID")
        seen.add(key)
        item = by_key[key]
        item["required"] = item["required"] or update["required"]
        item["sensitive"] = item["sensitive"] or update["sensitive"]
        if item["sensitive"]:
            item["public"] = False
    return {
        "deploymentPlan": plan,
        "manifestCandidate": manifest_candidate_from_plan(plan),
    }


def _include_prompt_file(relative: str) -> bool:
    path = Path(relative)
    if not _SAFE_PATH.fullmatch(relative) or any(_PRIVATE_ENV.fullmatch(part) for part in path.parts):
        return False
    if _SECRET_NAME.search(path.name):
        return False
    return (
        path.name in _PROMPT_FILENAMES
        or path.name in _ENTRYPOINT_FILENAMES
        or path.suffix.lower() in _PROMPT_SUFFIXES
    )


def _prompt_priority(relative: str) -> tuple[int, int, str]:
    path = Path(relative)
    if path.name in {"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml", "Dockerfile"}:
        tier = 0
    elif path.name in {"package.json", "pyproject.toml", "requirements.txt", "Procfile"}:
        tier = 1
    elif path.name in _ENTRYPOINT_FILENAMES:
        tier = 2
    elif path.name in {"README", "README.md"}:
        tier = 3
    else:
        tier = 4
    return tier, len(path.parts), relative


def _assert_environment_not_weakened(
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
    *,
    service_keys: set[str],
    service_environment: Mapping[str, set[str]],
) -> None:
    proposed = {(item["name"], item["scope"]): item for item in candidate}
    if len(proposed) != len(candidate):
        raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")
    if set(proposed) != {(item["name"], item["scope"]) for item in baseline}:
        raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")
    for item in baseline:
        current = proposed.get((item["name"], item["scope"]))
        if current is None or not set(item["services"]) <= set(current["services"]):
            raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")
        for boolean in ("required", "sensitive", "public"):
            if item[boolean] and not current[boolean]:
                raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")
        if current["configured"]:
            raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")
    for item in candidate:
        if (
            not _ENV_NAME.fullmatch(item["name"])
            or item["configured"]
            or not set(item["services"]) <= service_keys
            or item["scope"] not in {"build", "runtime"}
            or any(
                not isinstance(item[field], bool)
                for field in ("required", "sensitive", "public", "configured")
            )
        ):
            raise AIDiagnosticError("AI_PROPOSAL_INVALID")
        if _SECRET_NAME.search(item["name"]) and (
            not item["sensitive"] or item["public"]
        ):
            raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")
    names_by_service: dict[str, set[str]] = {key: set() for key in service_keys}
    for item in candidate:
        for service in item["services"]:
            names_by_service[service].add(item["name"])
    for service in service_keys:
        if not service_environment[service] <= names_by_service[service]:
            raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")


def _validate_plan_relationships(plan: Mapping[str, Any], build_plan: Mapping[str, Any]) -> None:
    service_by_key = {item["key"]: item for item in plan["services"]}
    build_keys = {item["key"] for item in build_plan["builds"]}
    external = {item["key"]: item for item in build_plan["externalImages"]}
    for key, service in service_by_key.items():
        image = service["image"]
        if image["source"] == "build" and image.get("buildKey") not in build_keys:
            raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")
        if image["source"] == "external" and (
            key not in external or external[key]["ref"] != image.get("ref")
        ):
            raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")
    if sum(bool(route["primary"]) for route in plan["routes"]) != bool(plan["routes"]):
        raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")
    for route in plan["routes"]:
        service = service_by_key.get(route["serviceKey"])
        if not service or service["role"] != "http" or service.get("port") != route["containerPort"]:
            raise AIDiagnosticError("AI_PROPOSAL_UNSAFE")


def _normalize_manifest_candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"schemaVersion", "kind", "region", "services", "volumes", "domainAssignment"}
    if set(value) != allowed:
        raise AIDiagnosticError("AI_MANIFEST_CANDIDATE_INVALID")
    if (
        value.get("schemaVersion") != MANIFEST_CANDIDATE_SCHEMA
        or value.get("kind") not in {"service", "compose"}
        or value.get("region") != "cn"
        or value.get("domainAssignment") != "platform-random-itool-tech"
        or not isinstance(value.get("services"), list)
        or not isinstance(value.get("volumes"), list)
    ):
        raise AIDiagnosticError("AI_MANIFEST_CANDIDATE_INVALID")
    return copy.deepcopy(dict(value))


def _remediation(code: str) -> str:
    exact = {
        "PUBLIC_TCP_UNSUPPORTED": "Remove the public TCP route or expose the service through HTTP.",
        "PUBLIC_UDP_UNSUPPORTED": "Remove the public UDP route; LAE currently publishes HTTP only.",
        "COMPOSE_HOST_BIND": "Replace the host bind mount with a managed named volume.",
        "COMPOSE_DOCKER_SOCKET": "Remove the Docker socket mount.",
        "COMPOSE_PRIVILEGED": "Remove privileged mode and use ordinary container permissions.",
        "COMPOSE_HOST_PORT": "Remove the host-side published port; keep only the container port.",
    }
    return exact.get(code, "Remove or replace the unsupported field, then run analysis again.")
