from __future__ import annotations

"""Render a Luma ServiceSpec into a Nomad job (API JSON format).

It emits the ``{"Job": {...}}`` structure consumed by ``nomad job run -json``
and the ``PUT /v1/jobs`` HTTP API, so the same artifact works from the CLI and
from Luma Control.

Exposure mapping:
  - cn-edge / external-edge : Nomad-native service + Traefik nomad-provider
    tags (Traefik discovers and routes); dynamic port by default, or a static
    ReservedPort when publishPort is explicit.
  - tailscale-relay / tcp-relay : by default docker host mode for
    macOS/OrbStack; if publishPort is explicit, bridge + ReservedPorts for
    Linux port mapping.
  - cloudflare-tunnel : app task + cloudflared sidecar task in one group.
  - none : worker, no service/port unless the manifest declares one.
"""

import json
import math
import re
import urllib.parse
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping

from .config import LumaConfig
from .errors import LumaError
from .registry import normalize_registry_host
from .service import ServiceSpec, slugify, tcp_entrypoint_name, tcp_relay_publish_port

# Nomad requires CPU (MHz) and MemoryMB on every task. These match Nomad's own
# defaults so an unspecified manifest behaves like a small container.
DEFAULT_CPU_MHZ = 100
DEFAULT_MEMORY_MB = 256

# Control owns persistent orchestration state, active WebSocket sessions, and
# streamed Builder progress.  A tenant-sized 256 MiB hard limit is too small:
# serializing a multi-megabyte control.json while a build stream is active can
# exceed it and make every public Control route return 502 until Nomad restarts
# the task.  Reserve enough for the steady state and retain bounded burst room.
CONTROL_CPU_MHZ = 500
CONTROL_MEMORY_MB = 1024
# Nomad ignores MemoryMaxMB unless memory oversubscription is enabled. Keep the
# hard reservation equal to the actual Control ceiling so an OOM fix cannot be
# silently discarded by the scheduler.
CONTROL_MEMORY_MAX_MB = 0

# Traefik's default entrypoint read timeout is 60 seconds and covers the whole
# request body. BuildKit may finalize a large registry layer with one PUT, so a
# valid push can exceed that limit even while bytes are continuously flowing.
# Keep a finite boundary for public ingress, but make it large enough for a
# multi-gigabyte *single layer* on a slow CI uplink. Six hours also matches the
# practical upper bound of common CI jobs; unlike ``0`` it does not turn an
# authenticated registry requirement into an unbounded public slow-body slot.
TRAEFIK_WEBSECURE_READ_TIMEOUT = "6h"

EDGE_EXPOSURES = {"cn-edge", "external-edge"}
HOST_PORT_EXPOSURES = {"tailscale-relay", "tcp-relay"}
NOMAD_TAILSCALE_META_KEY = "luma_tailscale_ip"
NOMAD_TAILSCALE_SERVICE_ADDRESS = f"${{meta.{NOMAD_TAILSCALE_META_KEY}}}"
NOMAD_LOCAL_HOST_SERVICE_ADDRESS = "__luma_nomad_host__"

CONTROL_JOB_FILE_ENV_NAMES = frozenset(
    {
        "LUMA_LAE_SERVICE_PRINCIPALS_FILE",
        "LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_FILE",
        "LUMA_CREDENTIAL_BROKER_TOKEN_FILE",
        "LUMA_OBJECT_SOURCE_BROKER_TOKEN_FILE",
        "LUMA_LAE_ADMIN_TOKEN_FILE",
        "LUMA_LAE_PLAN_SIGNING_KEYS_FILE",
    }
)
CONTROL_JOB_URL_ENV_NAMES = frozenset(
    {
        "LUMA_CREDENTIAL_BROKER_URL",
        "LUMA_OBJECT_SOURCE_BROKER_URL",
        "LUMA_LAE_ADMIN_API_URL",
    }
)
CONTROL_JOB_TIMEOUT_ENV_NAMES = frozenset(
    {
        "LUMA_CREDENTIAL_BROKER_TIMEOUT_SECONDS",
        "LUMA_OBJECT_SOURCE_BROKER_TIMEOUT_SECONDS",
        "LUMA_LAE_ADMIN_TIMEOUT_SECONDS",
    }
)
CONTROL_JOB_LAE_CONFIG_ENV_NAMES = frozenset(
    {
        "LUMA_BUILDER_ANALYZE_IMAGE_DIGEST",
        "LUMA_LAE_BUILDER_ALLOW_ANONYMOUS_REGISTRY",
        "LUMA_LAE_BUILDER_ALLOW_BASIC_REGISTRY",
        "LUMA_LAE_BUILDER_REGISTRY_INSECURE",
        "LUMA_LAE_BUILDER_EXTERNAL_REGISTRIES_JSON",
        "LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON",
        "LUMA_LAE_RUNTIME_STORAGE_CLASS",
        "LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS",
    }
)
CONTROL_JOB_ENV_ALLOWLIST = frozenset(
    {
        *CONTROL_JOB_FILE_ENV_NAMES,
        *CONTROL_JOB_URL_ENV_NAMES,
        *CONTROL_JOB_TIMEOUT_ENV_NAMES,
        *CONTROL_JOB_LAE_CONFIG_ENV_NAMES,
    }
)


def _control_job_file_path(value: str) -> str:
    candidate = value.strip()
    if (
        candidate != value
        or not candidate
        or len(candidate) > 1024
        or any(character in candidate for character in ("\0", "\n", "\r"))
    ):
        raise LumaError("Luma Control file path is invalid")
    path = PurePosixPath(candidate)
    raw_parts = candidate.split("/")
    if (
        not path.is_absolute()
        or path == PurePosixPath("/opt/luma/control")
        or path.parts[:4] != ("/", "opt", "luma", "control")
        or any(part in {"", ".", ".."} for part in raw_parts[1:])
    ):
        raise LumaError(
            "Luma Control file paths must stay within /opt/luma/control"
        )
    return candidate


def _control_job_https_url(name: str, value: str) -> str:
    candidate = value.strip()
    if (
        candidate != value
        or not candidate
        or len(candidate) > 2048
        or any(character in candidate for character in ("\0", "\n", "\r", " ", "\t"))
    ):
        raise LumaError(f"{name} must be a closed HTTPS URL")
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except ValueError:
        raise LumaError(f"{name} must be a closed HTTPS URL") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
        or name == "LUMA_LAE_ADMIN_API_URL" and parsed.path not in {"", "/"}
    ):
        raise LumaError(f"{name} must be a closed HTTPS URL")
    return candidate.rstrip("/") if parsed.path == "/" else candidate


def _control_job_timeout(name: str, value: str) -> str:
    candidate = value.strip()
    try:
        timeout = float(candidate)
    except (TypeError, ValueError):
        raise LumaError(f"{name} is invalid") from None
    minimum = 1.0 if name == "LUMA_LAE_ADMIN_TIMEOUT_SECONDS" else 0.1
    if (
        candidate != value
        or not candidate
        or len(candidate) > 32
        or not math.isfinite(timeout)
        or not minimum <= timeout <= 30.0
    ):
        raise LumaError(f"{name} is invalid")
    return candidate


def _control_job_lae_config(name: str, value: str) -> str:
    """Validate non-secret LAE policy values copied into Luma Control."""

    candidate = value.strip()
    if candidate != value or not candidate or len(candidate) > 4096:
        raise LumaError(f"{name} is invalid")
    if name == "LUMA_BUILDER_ANALYZE_IMAGE_DIGEST":
        if re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", candidate) is None:
            raise LumaError(f"{name} must be an immutable image digest")
        return candidate
    if name in {
        "LUMA_LAE_BUILDER_ALLOW_ANONYMOUS_REGISTRY",
        "LUMA_LAE_BUILDER_ALLOW_BASIC_REGISTRY",
        "LUMA_LAE_BUILDER_REGISTRY_INSECURE",
    }:
        if candidate not in {"0", "1"}:
            raise LumaError(f"{name} must be 0 or 1")
        return candidate
    if name == "LUMA_LAE_BUILDER_EXTERNAL_REGISTRIES_JSON":
        try:
            registries = json.loads(candidate)
        except json.JSONDecodeError:
            raise LumaError(f"{name} is invalid") from None
        if (
            not isinstance(registries, list)
            or len(registries) > 32
            or any(not isinstance(item, str) for item in registries)
        ):
            raise LumaError(f"{name} is invalid")
        try:
            normalized = [normalize_registry_host(item) for item in registries]
        except LumaError:
            raise LumaError(f"{name} is invalid") from None
        if (
            normalized != registries
            or len(set(normalized)) != len(normalized)
            or normalized != sorted(normalized)
        ):
            raise LumaError(f"{name} must be a sorted exact registry list")
        return json.dumps(normalized, separators=(",", ":"), ensure_ascii=True)
    if name == "LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON":
        try:
            nodes = json.loads(candidate)
        except json.JSONDecodeError:
            raise LumaError(f"{name} is invalid") from None
        if (
            not isinstance(nodes, list)
            or not nodes
            or len(nodes) > 64
            or any(not isinstance(item, str) for item in nodes)
            or len(nodes) != len(set(nodes))
            or nodes != sorted(nodes)
            or any(
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item)
                is None
                for item in nodes
            )
        ):
            raise LumaError(f"{name} must be a sorted exact node list")
        return json.dumps(nodes, separators=(",", ":"), ensure_ascii=True)
    if name == "LUMA_LAE_RUNTIME_STORAGE_CLASS":
        if re.fullmatch(r"[a-z][a-z0-9-]{0,62}", candidate) is None:
            raise LumaError(f"{name} is invalid")
        return candidate
    if name == "LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS":
        try:
            timeout = int(candidate)
        except ValueError:
            raise LumaError(f"{name} is invalid") from None
        if str(timeout) != candidate or not 30 <= timeout <= 3600:
            raise LumaError(f"{name} is invalid")
        return candidate
    raise LumaError(f"{name} is not an allowlisted LAE Control setting")


def control_job_environment(values: Mapping[str, str] | None) -> Dict[str, str]:
    """Return the only host values allowed into the luma-control Nomad task."""

    result: Dict[str, str] = {}
    source = values or {}
    for name in sorted(CONTROL_JOB_ENV_ALLOWLIST):
        raw = source.get(name)
        if raw is None or str(raw) == "":
            continue
        value = str(raw)
        if name in CONTROL_JOB_FILE_ENV_NAMES:
            result[name] = _control_job_file_path(value)
        elif name in CONTROL_JOB_URL_ENV_NAMES:
            result[name] = _control_job_https_url(name, value)
        elif name in CONTROL_JOB_TIMEOUT_ENV_NAMES:
            result[name] = _control_job_timeout(name, value)
        else:
            result[name] = _control_job_lae_config(name, value)
    return result


def uses_traefik_tags(service: ServiceSpec) -> bool:
    return service.exposure in EDGE_EXPOSURES


def render_traefik_job(
    *,
    image: str,
    nomad_addr: str = "http://127.0.0.1:4646",
    acme_email: str = "",
    cert_resolver: str = "letsencrypt",
    acme_dns_provider: str = "",
    acme_dns_token_file: str = "",
    acme_domains: list[str] | None = None,
    tcp_entrypoints: list[int] | None = None,
    as_json: bool = True,
) -> str | Dict[str, Any]:
    """Render the Traefik ingress job (host mode, ports 80/443 + tcp entrypoints).

    Host networking: Traefik binds 80/443 and reaches every file-route backend
    (Tailscale IPs) directly. letsencrypt certs persist via a NAMED VOLUME mount
    block (NOT the docker `volumes` shorthand — that lands them in the ephemeral
    alloc dir and forces a full cert re-request on every restart -> ACME rate
    limit). Routes dir is a host bind. Providers: file + nomad.
    """
    args = [
        "--providers.file.directory=/dynamic",
        "--providers.file.watch=true",
        "--providers.nomad=true",
        f"--providers.nomad.endpoint.address={nomad_addr}",
        "--providers.nomad.exposedByDefault=false",
        "--providers.nomad.watch=true",
        "--accesslog=true",
        "--accesslog.format=json",
        "--entrypoints.web.address=:80",
        "--entrypoints.websecure.address=:443",
        (
            "--entrypoints.websecure.transport.respondingTimeouts.readTimeout="
            f"{TRAEFIK_WEBSECURE_READ_TIMEOUT}"
        ),
        "--entrypoints.web.http.redirections.entrypoint.to=websecure",
        "--entrypoints.web.http.redirections.entrypoint.scheme=https",
    ]
    for port in tcp_entrypoints or []:
        args.append(f"--entrypoints.tcp-{int(port)}.address=:{int(port)}")
    if acme_email:
        args.extend([
            f"--certificatesresolvers.{cert_resolver}.acme.email={acme_email}",
            f"--certificatesresolvers.{cert_resolver}.acme.storage=/letsencrypt/acme.json",
        ])
        if acme_dns_provider:
            args.extend([
                f"--certificatesresolvers.{cert_resolver}.acme.dnschallenge=true",
                f"--certificatesresolvers.{cert_resolver}.acme.dnschallenge.provider={acme_dns_provider}",
            ])
        else:
            args.extend([
                f"--certificatesresolvers.{cert_resolver}.acme.httpchallenge=true",
                f"--certificatesresolvers.{cert_resolver}.acme.httpchallenge.entrypoint=web",
            ])
    args.append("--api.dashboard=true")
    job = {
        "ID": "traefik", "Name": "traefik", "Type": "service", "Datacenters": ["dc1"],
        "Constraints": [{"LTarget": "${meta.ingress}", "RTarget": "true", "Operand": "="}],
        "Update": {"AutoRevert": True, "MinHealthyTime": 5_000_000_000, "HealthyDeadline": 120_000_000_000},
        "TaskGroups": [{
            "Name": "traefik", "Count": 1, "MaxClientDisconnect": 3_600_000_000_000,
            "Tasks": [{
                "Name": "traefik", "Driver": "docker",
                "Config": {
                    "image": image,
                    "network_mode": "host",
                    "args": args,
                    "mount": [
                        {"type": "volume", "target": "/letsencrypt", "source": "traefik_traefik_letsencrypt"},
                        {"type": "bind", "target": "/dynamic", "source": "/opt/luma/routes"},
                    ],
                },
                "Resources": {"CPU": 200, "MemoryMB": 256},
            }],
        }],
        "Meta": {"luma.managed": "true"},
    }
    task = job["TaskGroups"][0]["Tasks"][0]
    if acme_dns_provider == "cloudflare" and acme_dns_token_file:
        task["Env"] = {"CF_DNS_API_TOKEN_FILE": "/run/secrets/cloudflare-dns-token"}
        task["Config"]["mount"].append({
            "type": "bind",
            "target": "/run/secrets/cloudflare-dns-token",
            "source": acme_dns_token_file,
            "readonly": True,
        })
    for index, domain in enumerate(acme_domains or []):
        clean = str(domain).strip().strip(".")
        if not clean:
            continue
        args.extend([
            f"--entrypoints.websecure.http.tls.domains[{index}].main={clean}",
            f"--entrypoints.websecure.http.tls.domains[{index}].sans=*.{clean}",
        ])
    wrapped = {"Job": job}
    return json.dumps(wrapped, indent=2, ensure_ascii=False) if as_json else wrapped


def render_egress_job(
    *,
    image: str,
    config_dir: str = "/opt/luma/egress-gateway",
    proxy_port: int = 7890,
    as_json: bool = True,
) -> str | Dict[str, Any]:
    """Render the egress (mihomo) proxy job: bridge + static 7890, config bind."""
    job = {
        "ID": "egress", "Name": "egress", "Type": "service", "Datacenters": ["dc1"],
        "Constraints": [{"LTarget": "${meta.egress}", "RTarget": "true", "Operand": "="}],
        "Update": {"AutoRevert": True, "MinHealthyTime": 5_000_000_000, "HealthyDeadline": 120_000_000_000},
        "TaskGroups": [{
            "Name": "egress", "Count": 1, "MaxClientDisconnect": 3_600_000_000_000,
            "Networks": [{"Mode": "bridge", "ReservedPorts": [
                {"Label": "proxy", "Value": int(proxy_port), "To": int(proxy_port), "HostNetwork": "default"}
            ]}],
            "Tasks": [{
                "Name": "mihomo", "Driver": "docker",
                "Config": {
                    "image": image,
                    "ports": ["proxy"],
                    "args": ["-d", config_dir],
                    "mount": [{"type": "bind", "target": "/root/.config/mihomo", "source": config_dir}],
                },
                "Resources": {"CPU": 200, "MemoryMB": 256},
            }],
        }],
        "Meta": {"luma.managed": "true"},
    }
    wrapped = {"Job": job}
    return json.dumps(wrapped, indent=2, ensure_ascii=False) if as_json else wrapped


def _resolve_env_value(value: Any, *, secrets: Mapping[str, str] | None = None, resolve_secrets: bool = True) -> str:
    """Substitute ${VAR} references from the caller-supplied secrets mapping.

    The Nomad job Env block is literal, so there is no deploy-time ${VAR}
    substitution layer after registration. Secrets are passed in explicitly by
    the caller (Luma Control builds the merged global+scoped map per deploy);
    render never reads process state, so it is a pure function and safe under
    concurrent deploys.
    """
    if not resolve_secrets:
        return str(value)

    available = secrets or {}

    def repl(m: "re.Match[str]") -> str:
        name = m.group(1)
        v = available.get(name)
        if v is None:
            raise LumaError(f"missing deployment secret: {name}. Run: luma secret set {name}")
        return v

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, str(value))


def render_compose_job(
    config: LumaConfig,
    deployment: Any,  # ComposeDeploymentSpec
    *,
    as_json: bool = True,
    registry_auth_resolver: Any | None = None,
    secrets: Mapping[str, str] | None = None,
    resolve_secrets: bool = True,
    egress_proxy_url: str | None = None,
) -> str | Dict[str, Any]:
    """Render a Luma compose deployment into a single multi-task Nomad job.

    All compose services become tasks in ONE group so they share a network
    namespace (bridge mode). Inter-service references like `tcp(mysql:3306)`
    keep working via per-task extra_hosts that map every service name to
    127.0.0.1 — so the user's docker-compose DSNs need no rewrite. Secrets
    (${VAR}) are resolved from the caller-supplied secrets mapping at render
    time. Named volumes use
    docker mount blocks (never the volumes shorthand). Exposed ports are
    published via group ReservedPorts matching the file-provider routes.

    The group runs on one node (Nomad schedules a group atomically); if compose
    services pin to different nodes this raises.
    """
    compose = deployment.compose
    services = compose.get("services") if isinstance(compose.get("services"), dict) else {}
    if not services:
        raise LumaError("compose deployment has no services to render")
    name = deployment.slug

    region = deployment.region
    regions: set[str] = set()
    nodes: set[str] = set()
    for override in deployment.services.values():
        if override.region:
            regions.add(override.region)
        if override.node:
            nodes.add(override.node)
    if len(regions) > 1:
        raise LumaError(
            f"compose deployment {name} pins services to multiple regions {sorted(regions)}; "
            "a Nomad group runs in one region — use separate deployments"
        )
    if regions:
        region = next(iter(regions))
    if len(nodes) > 1:
        raise LumaError(
            f"compose deployment {name} pins services to multiple nodes {sorted(nodes)}; "
            "a Nomad group runs on one node — use separate deployments"
        )
    pinned_node = next(iter(nodes), "")
    service_address = _node_service_address(config, pinned_node) if pinned_node else ""
    constraints = [{"LTarget": "${meta.region}", "RTarget": region, "Operand": "="}]
    if nodes:
        constraints.append({"LTarget": "${meta.luma_node_name}", "RTarget": pinned_node, "Operand": "="})

    # service name -> 127.0.0.1 so DSNs referencing sibling service names resolve
    # over the shared group loopback.
    extra_hosts = [f"{svc}:127.0.0.1" for svc in services.keys()]

    reserved_ports: List[Dict[str, Any]] = []
    dynamic_ports: List[Dict[str, Any]] = []
    nomad_services: List[Dict[str, Any]] = []
    tasks: List[Dict[str, Any]] = []

    for svc_name, body in services.items():
        if not isinstance(body, dict):
            raise LumaError(f"compose service {svc_name} must be a mapping")
        image = str(body.get("image") or "")
        if not image:
            raise LumaError(f"compose service {svc_name} requires an image")
        override = deployment.services.get(str(svc_name))
        label = re.sub(r"[^a-zA-Z0-9_]", "_", str(svc_name))
        exposure = override.exposure if override else "none"
        port = override.port if override else None
        publish_port = override.publish_port if override else None
        publish = publish_port or port

        docker_config: Dict[str, Any] = {"image": image, "extra_hosts": list(extra_hosts)}
        registry_auth = registry_auth_resolver(image) if callable(registry_auth_resolver) else None
        if registry_auth and registry_auth.get("username") and registry_auth.get("password"):
            docker_config["auth"] = {
                "username": str(registry_auth["username"]),
                "password": str(registry_auth["password"]),
                "server_address": str(registry_auth.get("serveraddress") or registry_auth.get("serverAddress") or ""),
            }
        if body.get("command") is not None:
            docker_config["args"] = _as_args(body["command"])
        if port and exposure in {"tcp-relay", "tailscale-relay", "cn-edge", "external-edge"}:
            if exposure in EDGE_EXPOSURES and publish_port is None:
                dynamic_ports.append({"Label": label, "To": int(port), "HostNetwork": "default"})
            else:
                reserved_ports.append({"Label": label, "Value": int(publish), "To": int(port), "HostNetwork": "default"})
            docker_config["ports"] = [label]
        mounts: List[Dict[str, Any]] = []
        for vspec in (body.get("volumes") or []):
            if isinstance(vspec, dict):
                # docker-compose long/expanded volume syntax, e.g.
                #   {type: volume, source: data, target: /var/lib/mysql}
                #   {type: bind, source: /srv/data, target: /etc/app, read_only: true}
                # This is what `docker compose config` emits when normalizing, so
                # a user copying a real compose file routinely hits it. Luma's own
                # compose validation already understands the dict form, so we must
                # too — stringifying and splitting on ':' would mangle it into a
                # nonsensical mount (source/target become dict-repr fragments).
                vtype = str(vspec.get("type") or "").strip().lower()
                src = str(vspec.get("source") or "").strip()
                tgt = str(vspec.get("target") or "").strip()
                if not src:
                    # anonymous volume / tmpfs: no host source to bind, drop it
                    continue
                if not tgt:
                    raise LumaError(
                        f"compose service {svc_name} has an invalid volume mapping "
                        f"(missing target): {vspec!r}"
                    )
                if vtype not in ("volume", "bind"):
                    vtype = "bind" if src[0] in ("/", ".", "~") else "volume"
                mounts.append({
                    "type": vtype,
                    "source": src,
                    "target": tgt,
                    "readonly": bool(vspec.get("read_only")),
                })
                continue
            parts = str(vspec).split(":")
            if len(parts) == 1:
                # docker-compose short syntax: a bare container path is an
                # anonymous volume (e.g. "- /var/lib/mysql"). It has no host
                # source to bind, so it cannot become a Nomad mount; drop it
                # rather than aborting the whole deploy. Only an explicit
                # source:target form with a missing half is malformed.
                continue
            if not parts[0] or not parts[1]:
                raise LumaError(
                    f"compose service {svc_name} has an invalid volume spec "
                    f"(expected source:target): {vspec!r}"
                )
            src, tgt = parts[0], parts[1]
            is_path = src[0] in ("/", ".", "~")
            mounts.append({
                "type": "bind" if is_path else "volume",
                "source": src,
                "target": tgt,
                "readonly": len(parts) > 2 and parts[2] == "ro",
            })
        if mounts:
            docker_config["mount"] = mounts

        env: Dict[str, str] = {}
        raw_env = body.get("environment")
        if isinstance(raw_env, dict):
            for k, v in raw_env.items():
                env[str(k)] = _resolve_env_value(v, secrets=secrets, resolve_secrets=resolve_secrets)
        elif isinstance(raw_env, list):
            for item in raw_env:
                k, _, v = str(item).partition("=")
                env[k] = _resolve_env_value(v, secrets=secrets, resolve_secrets=resolve_secrets)
        if override and override.proxy and egress_proxy_url:
            # See _app_task: inject the real egress gateway address, never a
            # *.service.consul name (Luma runs no Consul → it never resolves).
            env.setdefault("HTTP_PROXY", egress_proxy_url)
            env.setdefault("HTTPS_PROXY", egress_proxy_url)

        resources = {"CPU": DEFAULT_CPU_MHZ, "MemoryMB": DEFAULT_MEMORY_MB}
        rc = (body.get("deploy") or {}).get("resources") or {}
        limits = rc.get("limits") or {}
        reservations = rc.get("reservations") or {}
        cpus_val = limits.get("cpus") or reservations.get("cpus")
        mem_val = limits.get("memory") or reservations.get("memory")
        if cpus_val is not None:
            resources["CPU"] = _cpu_mhz(cpus_val)
        if mem_val is not None:
            resources["MemoryMB"] = _memory_mb(mem_val)
            if limits.get("memory") and reservations.get("memory"):
                resources["MemoryMaxMB"] = _memory_mb(limits["memory"])
                resources["MemoryMB"] = _memory_mb(reservations["memory"])

        task: Dict[str, Any] = {"Name": str(svc_name), "Driver": "docker", "Config": docker_config, "Resources": resources}
        if env:
            task["Env"] = env
        tasks.append(task)

        if exposure in {"cn-edge", "external-edge"} and override and override.domain and port:
            # Compose service names are only unique inside one stack. Registering
            # every tenant's common `web`/`api` task under that raw name makes
            # Traefik merge unrelated allocations and gives every stack the same
            # router key. Keep the task and port label Compose-compatible, but
            # namespace the discovery service and router by the deployment slug.
            service_id = f"{name}-{slugify(str(svc_name))}"
            service_block = {
                "Name": service_id,
                "PortLabel": label,
                "Provider": "nomad",
                "Tags": [
                    "traefik.enable=true",
                    f"traefik.http.routers.{service_id}.rule=Host(`{override.domain}`)",
                    f"traefik.http.routers.{service_id}.entrypoints={config.entrypoint}",
                    f"traefik.http.routers.{service_id}.tls.certresolver={config.cert_resolver}",
                    f"traefik.http.routers.{service_id}.service={service_id}",
                ],
            }
            _set_edge_service_address(service_block, service_address)
            nomad_services.append(service_block)

    group: Dict[str, Any] = {
        "Name": name,
        "Count": 1,
        "MaxClientDisconnect": 3_600_000_000_000,
        "Tasks": tasks,
    }
    # A bridge Networks block makes Nomad put every task in the group into one
    # shared network namespace (via the pause container). That shared netns is
    # what makes the extra_hosts "service:127.0.0.1" mapping correct — sibling
    # DSNs like tcp(mysql:3306) resolve to the mysql task's loopback. Emit it
    # whenever the group has >1 task (siblings must reach each other) OR a
    # service publishes a port. Without it, an all-exposure:none multi-service
    # stack renders with no Networks block, each task gets its own loopback, and
    # inter-service connections silently fail while the deploy reports healthy.
    if len(tasks) > 1 or reserved_ports or dynamic_ports:
        network: Dict[str, Any] = {"Mode": "bridge"}
        if reserved_ports:
            network["ReservedPorts"] = reserved_ports
        if dynamic_ports:
            network["DynamicPorts"] = dynamic_ports
        group["Networks"] = [network]
    if nomad_services:
        group["Services"] = nomad_services
        if not service_address:
            constraints.append(_tailscale_service_address_constraint())

    job = {
        "ID": name, "Name": name, "Type": "service", "Datacenters": ["dc1"],
        "Constraints": constraints,
        "Update": _compose_update_stanza(deployment),
        "TaskGroups": [group],
        "Meta": {"luma.managed": "true", "luma.region": region, "luma.compose": "true"},
    }
    wrapped = {"Job": job}
    return json.dumps(wrapped, indent=2, ensure_ascii=False) if as_json else wrapped


def render_control_job(
    *,
    image: str,
    node_name: str,
    control_environment: Mapping[str, str] | None = None,
    as_json: bool = True,
) -> str | Dict[str, Any]:
    """Render the luma-control infrastructure job (bridge mode, port 8080).

    It mounts the manager's /opt/luma state + docker.sock as host binds (mount
    blocks, NOT the docker `volumes` shorthand; see _apply_volume_mounts for
    why). Pinned to the manager node. Routing is handled separately by the
    Traefik file route.
    """
    environment = {
        "DOCKER_API_VERSION": "1.44",
        "LUMA_CONTROL_CONFIG": "/opt/luma/luma.yaml",
        "LUMA_CONTROL_STATE_DIR": "/opt/luma/control",
        **control_job_environment(control_environment),
    }
    job = {
        "ID": "luma-control",
        "Name": "luma-control",
        "Type": "service",
        "Datacenters": ["dc1"],
        "Constraints": [{"LTarget": "${meta.luma_node_name}", "RTarget": node_name, "Operand": "="}],
        "Update": {
            "AutoRevert": True,
            "MinHealthyTime": 6_000_000_000,
            "HealthyDeadline": 120_000_000_000,
            "HealthCheck": "checks",
        },
        "TaskGroups": [{
            "Name": "luma-control",
            "Count": 1,
            "MaxClientDisconnect": 3_600_000_000_000,
            "Networks": [{"Mode": "bridge", "ReservedPorts": [{"Label": "http", "Value": 8080, "To": 8080}]}],
            "Services": [{
                "Name": "luma-control",
                "PortLabel": "http",
                "Provider": "nomad",
                "AddressMode": "host",
                "Checks": [{
                    "Name": "luma-control-health",
                    "Type": "http",
                    "PortLabel": "http",
                    "Path": "/v1/health",
                    "Interval": 10_000_000_000,
                    "Timeout": 2_000_000_000,
                }],
            }],
            "Tasks": [{
                "Name": "luma-control",
                "Driver": "docker",
                "Config": {
                    "image": image,
                    "ports": ["http"],
                    # Keep the complete Luma state tree on one bind mount.  Route
                    # files are staged in /opt/luma/.luma-route-staging and then
                    # renamed into /opt/luma/routes; separate nested bind mounts
                    # make that rename EXDEV and force staging back into the
                    # Traefik-watched directory.
                    "mount": [
                        {"type": "bind", "target": "/opt/luma", "source": "/opt/luma"},
                        {"type": "bind", "target": "/var/run/docker.sock", "source": "/var/run/docker.sock"},
                    ],
                },
                "Env": environment,
                "Resources": {
                    "CPU": CONTROL_CPU_MHZ,
                    "MemoryMB": CONTROL_MEMORY_MB,
                    "MemoryMaxMB": CONTROL_MEMORY_MAX_MB,
                },
            }],
        }],
        "Meta": {"luma.managed": "true"},
    }
    wrapped = {"Job": job}
    return json.dumps(wrapped, indent=2, ensure_ascii=False) if as_json else wrapped


def render_nomad_job(
    config: LumaConfig,
    service: ServiceSpec,
    *,
    datacenter: str = "dc1",
    as_json: bool = True,
    registry_auth: Dict[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
    resolve_secrets: bool = True,
    egress_proxy_url: str | None = None,
) -> str | Dict[str, Any]:
    """Render a ServiceSpec to a Nomad job. Returns JSON text (default) or the dict.

    registry_auth, when provided, is the {username, password, serveraddress} dict
    from Luma's managed credential store (registry_auth_for_image). It is injected
    into the app task's docker auth block so Nomad pulls private images.

    egress_proxy_url, when provided and the service has proxy: true, is injected
    as HTTP_PROXY/HTTPS_PROXY. The caller resolves the real gateway address; if
    omitted, no proxy env is set (render has no cluster state to derive it).
    """
    job = _build_job(
        config,
        service,
        datacenter=datacenter,
        registry_auth=registry_auth,
        secrets=secrets,
        resolve_secrets=resolve_secrets,
        egress_proxy_url=egress_proxy_url,
    )
    wrapped = {"Job": job}
    if as_json:
        return json.dumps(wrapped, indent=2, ensure_ascii=False)
    return wrapped


def _build_job(
    config: LumaConfig,
    service: ServiceSpec,
    *,
    datacenter: str,
    registry_auth: Dict[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
    resolve_secrets: bool = True,
    egress_proxy_url: str | None = None,
) -> Dict[str, Any]:
    name = service.slug

    constraints = _constraints(service)

    group: Dict[str, Any] = {
        "Name": name,
        "Count": service.replicas,
        # When a client's heartbeat times out (common for home nodes whose
        # Tailscale path falls back to a slow DERP relay), keep the local
        # allocations RUNNING instead of marking them lost and rescheduling.
        # They reconnect when the link recovers.
        "MaxClientDisconnect": 3_600_000_000_000,  # 1h in ns
    }

    network, port_label = _network(service)
    if network is not None:
        group["Networks"] = [network]

    nomad_service = _service_block(config, service, port_label)
    if nomad_service is not None:
        group["Services"] = [nomad_service]
        if nomad_service.get("Address") == NOMAD_TAILSCALE_SERVICE_ADDRESS:
            constraints.append(_tailscale_service_address_constraint())

    tasks = [_app_task(config, service, port_label, registry_auth=registry_auth, secrets=secrets, resolve_secrets=resolve_secrets, egress_proxy_url=egress_proxy_url)]
    sidecar = _cloudflared_task(service, secrets=secrets, resolve_secrets=resolve_secrets)
    if sidecar is not None:
        tasks.append(sidecar)
    group["Tasks"] = tasks

    job: Dict[str, Any] = {
        "ID": name,
        "Name": name,
        "Type": "service",
        "Datacenters": [datacenter],
        "Update": _update_stanza(service, has_service_check=bool(nomad_service and nomad_service.get("Checks"))),
        "TaskGroups": [group],
        "Meta": {"luma.managed": "true", "luma.region": service.region},
    }
    if constraints:
        job["Constraints"] = constraints
    return job


def _update_stanza(service: ServiceSpec, *, has_service_check: bool = False) -> Dict[str, Any]:
    update = {
        "AutoRevert": True,
        "MaxParallel": 1,
        "MinHealthyTime": 5_000_000_000,  # 5s in ns
        "HealthyDeadline": 120_000_000_000,  # 2m in ns
        "HealthCheck": "checks" if has_service_check else "task_states",
    }
    if _canary_before_promote(service):
        update["Canary"] = 1
        update["AutoPromote"] = True
    return update


def _compose_update_stanza(deployment: Any) -> Dict[str, Any]:
    update = {
        "AutoRevert": True,
        "MaxParallel": 1,
        "MinHealthyTime": 6_000_000_000,  # 6s in ns
        "HealthyDeadline": 180_000_000_000,  # 3m in ns
        "HealthCheck": "task_states",
    }
    if _compose_canary_before_promote(deployment):
        update["Canary"] = 1
        update["AutoPromote"] = True
    return update


def _canary_before_promote(service: ServiceSpec) -> bool:
    if service.replicas != 1:
        return False
    if service.exposure not in EDGE_EXPOSURES:
        return False
    return service.publish_port is None


def _compose_canary_before_promote(deployment: Any) -> bool:
    has_dynamic_edge = False
    for override in deployment.services.values():
        if override.publish_port is not None:
            return False
        if override.exposure in HOST_PORT_EXPOSURES:
            return False
        if override.exposure in EDGE_EXPOSURES and override.port:
            has_dynamic_edge = True
    if not has_dynamic_edge:
        return False

    services = deployment.compose.get("services") if isinstance(deployment.compose.get("services"), dict) else {}
    for body in services.values():
        if isinstance(body, dict) and body.get("volumes"):
            return False
    return True


def _constraints(service: ServiceSpec) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = [
        {"LTarget": "${meta.region}", "RTarget": service.region, "Operand": "="}
    ]
    if service.node:
        # Pin by node meta (set at agent-config time), stable across restarts.
        out.append({"LTarget": "${meta.luma_node_name}", "RTarget": service.node, "Operand": "="})
    for raw in service.constraints:
        parsed = _parse_user_constraint(raw)
        if parsed:
            out.append(parsed)
    return out


def _parse_user_constraint(raw: str) -> Dict[str, str] | None:
    """Accept a simple 'a == b' / 'a != b' user constraint and translate it."""
    text = str(raw).strip()
    for op_text, operand in (("==", "="), ("!=", "!=")):
        if op_text in text:
            left, right = text.split(op_text, 1)
            left, right = left.strip(), right.strip()
            if not left or not right:
                raise LumaError(f"invalid constraint: {raw!r}")
            # node.labels.X -> ${meta.X} so existing manifests keep working.
            attr = left
            if attr.startswith("node.labels."):
                attr = "${meta." + attr[len("node.labels.") :] + "}"
            return {"LTarget": attr, "RTarget": right, "Operand": operand}
    raise LumaError(f"unsupported constraint syntax (expected '==' or '!='): {raw!r}")


def _network(service: ServiceSpec) -> tuple[Dict[str, Any] | None, str | None]:
    """Return (network_block, port_label).

    Host-port exposures (tailscale-relay / tcp-relay) default to the docker
    driver's host networking (set in _app_task). Required on macOS/OrbStack:
    the agent runs on the Mac host but containers run in OrbStack's Linux VM, so
    any concrete HostIP Nomad fingerprints (a Mac NIC address) does not exist in
    the VM and the published port is unreachable. docker host mode sidesteps
    port mapping: the container uses the VM network stack and OrbStack bridges it
    to every Mac interface, Tailscale included. Trade-off: no port remapping, so
    the container's real port is exposed and two services on one node must use
    distinct real ports.

    On Linux nodes, manifests can opt into real port mapping by setting
    publishPort. That renders a bridge network with a ReservedPort mapping
    publishPort -> port, matching the current production path for lab/aly.

    Edge services keep a Nomad-managed port for Traefik nomad-provider discovery.
    """
    if service.exposure in HOST_PORT_EXPOSURES:
        if service.publish_port:
            if not service.port:
                raise LumaError(f"{service.exposure} requires port")
            label = "tcp" if service.exposure == "tcp-relay" else "http"
            net = {
                "Mode": "bridge",
                "ReservedPorts": [
                    {
                        "Label": label,
                        "Value": int(service.publish_port),
                        "To": int(service.port),
                        "HostNetwork": "default",
                    }
                ],
            }
            return net, label
        # No Nomad network block; _app_task sets docker network_mode=host.
        return None, None
    if service.exposure in EDGE_EXPOSURES:
        if not service.port:
            raise LumaError(f"{service.exposure} requires port")
        if service.publish_port:
            net = {
                "Mode": "bridge",
                "ReservedPorts": [
                    {
                        "Label": "http",
                        "Value": int(service.publish_port),
                        "To": int(service.port),
                        "HostNetwork": "default",
                    }
                ],
            }
        else:
            net = {"Mode": "host", "DynamicPorts": [{"Label": "http", "To": int(service.port)}]}
        return net, "http"
    # none / cloudflare-tunnel: only add a port if the manifest declares one.
    if service.port:
        if service.publish_port:
            # Fixed host port for internal services that must be reachable at a
            # known address (e.g. an in-cluster registry pulled by other nodes).
            net = {
                "Mode": "bridge",
                "ReservedPorts": [
                    {
                        "Label": "http",
                        "Value": int(service.publish_port),
                        "To": int(service.port),
                        "HostNetwork": "default",
                    }
                ],
            }
            return net, "http"
        net = {"Mode": "host", "DynamicPorts": [{"Label": "http", "To": int(service.port)}]}
        return net, "http"
    return None, None


def _service_block(config: LumaConfig, service: ServiceSpec, port_label: str | None) -> Dict[str, Any] | None:
    if not uses_traefik_tags(service):
        return None
    if port_label is None:
        return None
    name = service.slug
    tags = [
        "traefik.enable=true",
        f"traefik.http.routers.{name}.rule=Host(`{service.domain}`)",
        f"traefik.http.routers.{name}.entrypoints={config.entrypoint}",
        f"traefik.http.routers.{name}.tls.certresolver={config.cert_resolver}",
    ]
    tags.extend(str(t) for t in service.labels)
    block: Dict[str, Any] = {
        "Name": name,
        "PortLabel": port_label,
        "Provider": "nomad",
        "Tags": tags,
    }
    _set_edge_service_address(block, _node_service_address(config, service.node or ""))
    check = _health_check(service, port_label)
    if check is not None:
        block["Checks"] = [check]
    return block


def _set_edge_service_address(block: Dict[str, Any], address: str) -> None:
    # A scheduled node's default host address may be a provider-private LAN IP
    # (for example 10.0.0.10 on Tencent), which the manager-side Traefik cannot
    # reach. Pinned nodes can use their resolved configured address; otherwise
    # interpolate the Tailscale address published by every Luma Nomad client.
    if address == NOMAD_LOCAL_HOST_SERVICE_ADDRESS:
        # Traefik and the workload share the manager host. Let Nomad advertise
        # the actual host-network address bound to the dynamic port; overriding
        # it with the manager's Tailscale address produces a guaranteed 502
        # when the port binds to a provider/bridge host-network address.
        block.pop("Address", None)
        block["AddressMode"] = "host"
        return
    block["Address"] = address or NOMAD_TAILSCALE_SERVICE_ADDRESS
    block.pop("AddressMode", None)


def _tailscale_service_address_constraint() -> Dict[str, str]:
    # Fail closed on legacy nodes that have not received the metadata migration
    # yet: an unscheduled app is diagnosable, while a "running" app routed to an
    # unreachable private IP is a false success.
    return {"LTarget": NOMAD_TAILSCALE_SERVICE_ADDRESS, "Operand": "is_set"}


def _node_service_address(config: LumaConfig, node_name: str) -> str:
    if not node_name:
        return ""
    node = config.nodes.get(node_name)
    if node is None:
        return ""
    raw = node.raw or {}
    if bool(raw.get("lumaLocalIngress") or raw.get("localIngress")):
        return NOMAD_LOCAL_HOST_SERVICE_ADDRESS
    for key in ("tailscaleIP", "tailscaleIp", "tailscaleName", "advertiseAddr"):
        value = _clean_service_address(raw.get(key))
        if value:
            return value
    return _clean_service_address(node.public_ip)


def _clean_service_address(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        parsed = urllib.parse.urlparse(text)
        text = parsed.netloc or parsed.path
    if text.startswith("[") and "]" in text:
        return text[1:text.index("]")]
    if text.count(":") == 1:
        host, port = text.rsplit(":", 1)
        if port.isdigit():
            return host
    return text


def _health_check(service: ServiceSpec, port_label: str) -> Dict[str, Any] | None:
    hc = service.healthcheck or {}
    if not hc:
        return None
    test = hc.get("test")
    interval = _duration_ns(hc.get("interval", "30s"))
    timeout = _duration_ns(hc.get("timeout", "5s"))
    if isinstance(test, list) and test and test[0] in {"CMD", "CMD-SHELL"}:
        args = test[1:]
        url = _healthcheck_url(args)
        if url:
            parsed = urllib.parse.urlparse(url)
            return {
                "Type": "http",
                "Name": f"{service.slug}-health",
                "PortLabel": port_label,
                "Path": parsed.path or "/",
                "Interval": interval,
                "Timeout": timeout,
            }
    return {
        "Type": "tcp",
        "Name": f"{service.slug}-alive",
        "PortLabel": port_label,
        "Interval": interval,
        "Timeout": timeout,
    }


def _healthcheck_url(args: List[Any]) -> str:
    text = " ".join(str(arg) for arg in args)
    match = re.search(r"https?://[^'\"\s)]+", text)
    return match.group(0) if match else ""


def _app_task(
    config: LumaConfig,
    service: ServiceSpec,
    port_label: str | None,
    *,
    registry_auth: Dict[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
    resolve_secrets: bool = True,
    egress_proxy_url: str | None = None,
) -> Dict[str, Any]:
    docker_config: Dict[str, Any] = {"image": service.image}
    if registry_auth and registry_auth.get("username") and registry_auth.get("password"):
        # Pull private images with Luma's managed credentials.
        docker_config["auth"] = {
            "username": str(registry_auth["username"]),
            "password": str(registry_auth["password"]),
            "server_address": str(registry_auth.get("serveraddress") or registry_auth.get("serverAddress") or ""),
        }
    if service.exposure in HOST_PORT_EXPOSURES and port_label is None:
        # docker host networking: the container uses the client's network stack
        # directly and exposes its real listen port. The only mode that works on
        # macOS/OrbStack (see _network). The file-provider route must target the
        # container's real port (service.port), not a remapped publishPort.
        docker_config["network_mode"] = "host"
    elif port_label is not None:
        docker_config["ports"] = [port_label]
    if service.command is not None:
        docker_config["args"] = _as_args(service.command)
    _apply_volume_mounts(docker_config, service)

    env = {str(k): _resolve_env_value(v, secrets=secrets, resolve_secrets=resolve_secrets) for k, v in service.environment.items()}
    if service.proxy and egress_proxy_url:
        # Runtime egress proxy: route the container's outbound HTTP/HTTPS through
        # the egress (mihomo) gateway. The caller (Control) resolves the real
        # gateway address (http://<manager>:7890) since render has no cluster
        # state. We do NOT inject a *.service.consul name — Luma runs no Consul,
        # so that name never resolves and every proxied request fails silently.
        env.setdefault("HTTP_PROXY", egress_proxy_url)
        env.setdefault("HTTPS_PROXY", egress_proxy_url)

    task: Dict[str, Any] = {
        "Name": service.slug,
        "Driver": "docker",
        "Config": docker_config,
        "Resources": _resources(service),
    }
    if env:
        task["Env"] = env
    return task


def _cloudflared_task(service: ServiceSpec, *, secrets: Mapping[str, str] | None = None, resolve_secrets: bool = True) -> Dict[str, Any] | None:
    if service.exposure != "cloudflare-tunnel":
        return None
    token_env = service.tunnel.get("tokenEnv", "CLOUDFLARE_TUNNEL_TOKEN")
    return {
        "Name": "cloudflared",
        "Driver": "docker",
        "Config": {
            "image": "cloudflare/cloudflared:latest",
            "args": ["tunnel", "--no-autoupdate", "run"],
        },
        "Env": {"TUNNEL_TOKEN": _resolve_env_value("${" + token_env + "}", secrets=secrets, resolve_secrets=resolve_secrets)},
        "Resources": {"CPU": DEFAULT_CPU_MHZ, "MemoryMB": 128},
    }


def _apply_volume_mounts(docker_config: Dict[str, Any], service: ServiceSpec) -> None:
    """Map manifest volumes onto the docker driver's `mount` blocks.

    CRITICAL: use `mount` blocks (type=volume/bind), NOT the docker driver's
    `volumes` shorthand. Nomad interprets a "name:/path" entry in `volumes` as a
    path RELATIVE TO THE ALLOC DIR and bind-mounts an empty alloc subdirectory —
    so a service silently runs on EMPTY DATA while the real named volume sits
    untouched. (This bit us hard during migration: mysql/gitea came up on empty
    schemas.) `mount{type=volume,source=name}` attaches the actual named volume.
    Requires `plugin "docker" { config { volumes { enabled = true } } }` on the
    client (set by luma node join / render_agent_config).

    Named storageClass / NFS volumes will move to Nomad host_volume/CSI later.
    """
    if not service.volumes:
        return
    mounts: List[Dict[str, Any]] = []
    for spec in service.volumes:
        parts = str(spec).split(":")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise LumaError(f"invalid volume spec (expected source:target): {spec!r}")
        source, target = parts[0], parts[1]
        readonly = len(parts) > 2 and parts[2] == "ro"
        # A leading / . or ~ means a host path -> bind mount; otherwise a named volume.
        is_path = source[0] in ("/", ".", "~")
        mounts.append({
            "type": "bind" if is_path else "volume",
            "target": target,
            "source": source,
            "readonly": readonly,
        })
    docker_config["mount"] = mounts


def _resources(service: ServiceSpec) -> Dict[str, Any]:
    cpu = DEFAULT_CPU_MHZ
    mem = DEFAULT_MEMORY_MB
    res = service.resources or {}
    limits = res.get("limits") or {}
    reservations = res.get("reservations") or {}
    cpus_val = limits.get("cpus") or reservations.get("cpus")
    mem_val = limits.get("memory") or reservations.get("memory")
    if cpus_val is not None:
        cpu = _cpu_mhz(cpus_val)
    if mem_val is not None:
        mem = _memory_mb(mem_val)
    out = {"CPU": cpu, "MemoryMB": mem}
    if limits.get("memory") and reservations.get("memory"):
        # Luma reservations map to Nomad memory, and limits map to memory_max.
        out["MemoryMaxMB"] = _memory_mb(limits["memory"])
        out["MemoryMB"] = _memory_mb(reservations["memory"])
    return out


def _cpu_mhz(value: Any) -> int:
    try:
        cpus = float(value)
    except (TypeError, ValueError) as exc:
        raise LumaError(
            f"invalid resources.cpus value {value!r}: expected a number of CPU cores (e.g. 0.5, 2)"
        ) from exc
    if cpus <= 0:
        raise LumaError(f"invalid resources.cpus value {value!r}: must be greater than 0")
    return max(1, round(cpus * 1000))


def _memory_mb(value: Any) -> int:
    text = str(value).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGkmg]?)([Ii]?)[Bb]?", text)
    if not m:
        raise LumaError(f"cannot parse memory value: {value!r}")
    num = float(m.group(1))
    unit = m.group(2).upper()
    factor = {"": 1 / (1024 * 1024), "K": 1 / 1024, "M": 1.0, "G": 1024.0}[unit]
    return max(1, round(num * factor))


def _duration_ns(value: Any) -> int:
    text = str(value).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ns|us|ms|s|m|h)?", text)
    if not m:
        raise LumaError(f"cannot parse duration: {value!r}")
    num = float(m.group(1))
    unit = m.group(2) or "s"
    factor = {"ns": 1, "us": 1_000, "ms": 1_000_000, "s": 1_000_000_000,
              "m": 60_000_000_000, "h": 3_600_000_000_000}[unit]
    return int(num * factor)


def _as_args(command: Any) -> List[str]:
    if isinstance(command, list):
        return [str(c) for c in command]
    # A shell string -> run via sh -c so quoting behaves like Compose.
    return ["sh", "-c", str(command)]
