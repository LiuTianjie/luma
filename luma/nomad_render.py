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
import re
import urllib.parse
from typing import Any, Dict, List, Mapping

from .config import LumaConfig
from .errors import LumaError
from .service import ServiceSpec, tcp_entrypoint_name, tcp_relay_publish_port

# Nomad requires CPU (MHz) and MemoryMB on every task. These match Nomad's own
# defaults so an unspecified manifest behaves like a small container.
DEFAULT_CPU_MHZ = 100
DEFAULT_MEMORY_MB = 256

EDGE_EXPOSURES = {"cn-edge", "external-edge"}
HOST_PORT_EXPOSURES = {"tailscale-relay", "tcp-relay"}


def uses_traefik_tags(service: ServiceSpec) -> bool:
    return service.exposure in EDGE_EXPOSURES


def render_traefik_job(
    *,
    image: str,
    nomad_addr: str = "http://127.0.0.1:4646",
    acme_email: str = "",
    cert_resolver: str = "letsencrypt",
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
        "--entrypoints.web.address=:80",
        "--entrypoints.websecure.address=:443",
        "--entrypoints.web.http.redirections.entrypoint.to=websecure",
        "--entrypoints.web.http.redirections.entrypoint.scheme=https",
    ]
    for port in tcp_entrypoints or []:
        args.append(f"--entrypoints.tcp-{int(port)}.address=:{int(port)}")
    if acme_email:
        args.extend([
            f"--certificatesresolvers.{cert_resolver}.acme.email={acme_email}",
            f"--certificatesresolvers.{cert_resolver}.acme.storage=/letsencrypt/acme.json",
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
    constraints = [{"LTarget": "${meta.region}", "RTarget": region, "Operand": "="}]
    if nodes:
        constraints.append({"LTarget": "${meta.luma_node_name}", "RTarget": next(iter(nodes)), "Operand": "="})

    # service name -> 127.0.0.1 so DSNs referencing sibling service names resolve
    # over the shared group loopback.
    extra_hosts = [f"{svc}:127.0.0.1" for svc in services.keys()]

    reserved_ports: List[Dict[str, Any]] = []
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
        publish = (override.publish_port if override else None) or port

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
        if override and override.proxy:
            env.setdefault("HTTP_PROXY", "http://egress.service.consul:7890")
            env.setdefault("HTTPS_PROXY", "http://egress.service.consul:7890")

        resources = {"CPU": DEFAULT_CPU_MHZ, "MemoryMB": DEFAULT_MEMORY_MB}
        rc = (body.get("deploy") or {}).get("resources") or {}
        limits = rc.get("limits") or {}
        reservations = rc.get("reservations") or {}
        cpus_val = limits.get("cpus") or reservations.get("cpus")
        mem_val = limits.get("memory") or reservations.get("memory")
        if cpus_val is not None:
            resources["CPU"] = max(1, round(float(cpus_val) * 1000))
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
            nomad_services.append({
                "Name": str(svc_name),
                "PortLabel": label,
                "Provider": "nomad",
                "Tags": [
                    "traefik.enable=true",
                    f"traefik.http.routers.{label}.rule=Host(`{override.domain}`)",
                    f"traefik.http.routers.{label}.entrypoints={config.entrypoint}",
                    f"traefik.http.routers.{label}.tls.certresolver={config.cert_resolver}",
                ],
            })

    group: Dict[str, Any] = {
        "Name": name,
        "Count": 1,
        "MaxClientDisconnect": 3_600_000_000_000,
        "Tasks": tasks,
    }
    if reserved_ports:
        group["Networks"] = [{"Mode": "bridge", "ReservedPorts": reserved_ports}]
    if nomad_services:
        group["Services"] = nomad_services

    job = {
        "ID": name, "Name": name, "Type": "service", "Datacenters": ["dc1"],
        "Constraints": constraints,
        "Update": {"AutoRevert": True, "MinHealthyTime": 6_000_000_000, "HealthyDeadline": 180_000_000_000},
        "TaskGroups": [group],
        "Meta": {"luma.managed": "true", "luma.region": region, "luma.compose": "true"},
    }
    wrapped = {"Job": job}
    return json.dumps(wrapped, indent=2, ensure_ascii=False) if as_json else wrapped


def render_control_job(
    *,
    image: str,
    node_name: str,
    as_json: bool = True,
) -> str | Dict[str, Any]:
    """Render the luma-control infrastructure job (bridge mode, port 8080).

    It mounts the manager's /opt/luma state + docker.sock as host binds (mount
    blocks, NOT the docker `volumes` shorthand; see _apply_volume_mounts for
    why). Pinned to the manager node. Routing is handled separately by the
    Traefik file route.
    """
    job = {
        "ID": "luma-control",
        "Name": "luma-control",
        "Type": "service",
        "Datacenters": ["dc1"],
        "Constraints": [{"LTarget": "${meta.luma_node_name}", "RTarget": node_name, "Operand": "="}],
        "Update": {"AutoRevert": True, "MinHealthyTime": 6_000_000_000, "HealthyDeadline": 120_000_000_000},
        "TaskGroups": [{
            "Name": "luma-control",
            "Count": 1,
            "MaxClientDisconnect": 3_600_000_000_000,
            "Networks": [{"Mode": "bridge", "ReservedPorts": [{"Label": "http", "Value": 8080, "To": 8080}]}],
            "Tasks": [{
                "Name": "luma-control",
                "Driver": "docker",
                "Config": {
                    "image": image,
                    "ports": ["http"],
                    "mount": [
                        {"type": "bind", "target": "/opt/luma/control", "source": "/opt/luma/control"},
                        {"type": "bind", "target": "/opt/luma/luma.yaml", "source": "/opt/luma/luma.yaml"},
                        {"type": "bind", "target": "/opt/luma/routes", "source": "/opt/luma/routes"},
                        {"type": "bind", "target": "/opt/luma/stacks", "source": "/opt/luma/stacks"},
                        {"type": "bind", "target": "/var/run/docker.sock", "source": "/var/run/docker.sock"},
                    ],
                },
                "Env": {
                    "DOCKER_API_VERSION": "1.44",
                    "LUMA_CONTROL_CONFIG": "/opt/luma/luma.yaml",
                    "LUMA_CONTROL_STATE_DIR": "/opt/luma/control",
                },
                "Resources": {"CPU": 200, "MemoryMB": 256},
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
) -> str | Dict[str, Any]:
    """Render a ServiceSpec to a Nomad job. Returns JSON text (default) or the dict.

    registry_auth, when provided, is the {username, password, serveraddress} dict
    from Luma's managed credential store (registry_auth_for_image). It is injected
    into the app task's docker auth block so Nomad pulls private images.
    """
    job = _build_job(
        config,
        service,
        datacenter=datacenter,
        registry_auth=registry_auth,
        secrets=secrets,
        resolve_secrets=resolve_secrets,
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

    tasks = [_app_task(config, service, port_label, registry_auth=registry_auth, secrets=secrets, resolve_secrets=resolve_secrets)]
    sidecar = _cloudflared_task(service, secrets=secrets, resolve_secrets=resolve_secrets)
    if sidecar is not None:
        tasks.append(sidecar)
    group["Tasks"] = tasks

    # auto_revert is the headline new capability: a failed deploy rolls back to
    # the last healthy version on its own.
    update = {
        "AutoRevert": True,
        "MinHealthyTime": 5_000_000_000,   # 5s in ns
        "HealthyDeadline": 120_000_000_000,  # 2m in ns
    }
    if service.replicas > 1:
        update["MaxParallel"] = 1

    job: Dict[str, Any] = {
        "ID": name,
        "Name": name,
        "Type": "service",
        "Datacenters": [datacenter],
        "Update": update,
        "TaskGroups": [group],
        "Meta": {"luma.managed": "true", "luma.region": service.region},
    }
    if constraints:
        job["Constraints"] = constraints
    return job


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
    check = _health_check(service, port_label)
    if check is not None:
        block["Checks"] = [check]
    return block


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
    if service.proxy:
        # Runtime egress proxy: reach the egress service via Nomad-native
        # discovery.
        env.setdefault("HTTP_PROXY", "http://egress.service.consul:7890")
        env.setdefault("HTTPS_PROXY", "http://egress.service.consul:7890")

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
        cpu = max(1, round(float(cpus_val) * 1000))
    if mem_val is not None:
        mem = _memory_mb(mem_val)
    out = {"CPU": cpu, "MemoryMB": mem}
    if limits.get("memory") and reservations.get("memory"):
        # Luma reservations map to Nomad memory, and limits map to memory_max.
        out["MemoryMaxMB"] = _memory_mb(limits["memory"])
        out["MemoryMB"] = _memory_mb(reservations["memory"])
    return out


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
