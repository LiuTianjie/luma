"""Compile luma.yaml and luma.compose.yml + immutable BuildArtifact into K8s & Flux manifests."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Dict, Mapping

import yaml

from ...build_capabilities import infer_native_platform, validate_native_platform
from ...errors import LumaError
from ...regions import parse_region_name, region_uses_egress_proxy, validate_region_exposure
from ...service import TCP_RELAY_RESERVED_PORTS, VALID_EXPOSURES, slugify, tcp_entrypoint_name
from ..build.artifact_store import BuildArtifact

DEFAULT_OUTBOUND_PROXY_URL = "http://100.64.0.1:7890"
DEFAULT_NO_PROXY = (
    "localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
    "100.64.0.0/10,.svc,.svc.cluster.local,100.66.177.70,100.66.177.70:5000,gcode.gaojiua.com"
)


def _build_proxy_env(
    *,
    region: str,
    proxy_enabled: bool | None,
    proxy_url: str = DEFAULT_OUTBOUND_PROXY_URL,
) -> tuple[list[Dict[str, str]], str]:
    """Return (env_list, egress_mode) for Luma's region-aware egress & proxy: true feature."""
    use_proxy = bool(proxy_enabled) if proxy_enabled is not None else False
    egress_mode = "proxy" if (use_proxy or region_uses_egress_proxy(None, region)) else "direct"
    if not use_proxy:
        return [], egress_mode
    return [
        {"name": "HTTP_PROXY", "value": proxy_url},
        {"name": "HTTPS_PROXY", "value": proxy_url},
        {"name": "ALL_PROXY", "value": proxy_url},
        {"name": "NO_PROXY", "value": DEFAULT_NO_PROXY},
    ], "proxy"


def _extract_kubernetes_arch(platform: str) -> str:
    """Extract Kubernetes node architecture label (arm64, amd64) from platform string."""
    if not platform:
        return "amd64"
    norm = validate_native_platform(platform)
    arch = norm.split("/")[-1]
    if arch not in {"arm64", "amd64"}:
        raise LumaError(f"unsupported Kubernetes node architecture label: {arch!r}")
    return arch


def _build_region_node_selector(region: str, node_pin: str, arch: str = "") -> Dict[str, str]:
    """Compile Luma's region + node placement contract into Kubernetes nodeSelector labels."""
    selector: Dict[str, str] = {}
    if region:
        selector["luma.io/region"] = region
        selector["topology.kubernetes.io/region"] = region
    if node_pin:
        selector["kubernetes.io/hostname"] = node_pin
    if arch:
        norm_arch = arch.split("/")[-1] if "/" in arch else arch
        selector["kubernetes.io/arch"] = norm_arch
    return selector


def _validate_workload_architecture(
    platform: str,
    *,
    region: str = "",
    node_pin: str = "",
    node_architectures: Mapping[str, str] | None = None,
    service_name: str = "",
) -> str:
    """Validate platform against region and node placement; return normalized Kubernetes arch."""
    clean_platform = infer_native_platform(
        platform=platform,
        target_node=node_pin,
        target_region=region,
        node_architectures=node_architectures,
    )
    clean_platform = validate_native_platform(clean_platform)
    k8s_arch = _extract_kubernetes_arch(clean_platform)
    svc_prefix = f"service '{service_name}' " if service_name else ""

    clean_region = str(region or "").strip().lower()
    if clean_region == "home" and k8s_arch != "arm64":
        raise LumaError(
            f"workload architecture mismatch: {svc_prefix}platform {clean_platform} "
            f"conflicts with region home (Apple Silicon home cluster requires arm64); "
            f"cross-architecture emulation is disabled"
        )

    clean_node = str(node_pin or "").strip()
    if clean_node:
        lower_node = clean_node.lower()
        if lower_node in {"macbook-pro", "air3"} and clean_region and clean_region != "home":
            raise LumaError(
                f"topology conflict: {svc_prefix}node {clean_node!r} belongs to region 'home', "
                f"but workload specifies region {clean_region!r}"
            )

        expected_arch = ""
        if node_architectures:
            node_arch = node_architectures.get(clean_node)
            if not node_arch and clean_node.lower() in node_architectures:
                node_arch = node_architectures[clean_node.lower()]
            if node_arch:
                norm_node = validate_native_platform(str(node_arch).strip())
                expected_arch = norm_node.split("/")[-1]

        if not expected_arch:
            if lower_node in {"macbook-pro", "air3"} or "arm64" in lower_node or "aarch64" in lower_node:
                expected_arch = "arm64"
            elif "amd64" in lower_node or "x86_64" in lower_node:
                expected_arch = "amd64"

        if expected_arch and k8s_arch != expected_arch:
            raise LumaError(
                f"architecture mismatch: {svc_prefix}node {clean_node} ({expected_arch}) "
                f"does not match image platform {clean_platform} ({k8s_arch})"
            )

    return k8s_arch


QUANTITY_UNIT_RE = re.compile(r"^\d+(?:\.\d+)?(?:[eE][-+]?\d+)?([kKMGTPE]i?|[m])$")


def _validate_storage_size(size: Any, vol_name: str) -> str:
    """Validate Kubernetes resource quantity units (e.g. 10Gi, 500Mi) per TC-B-F6-04."""
    if size is None or size == "":
        return "10Gi"
    if isinstance(size, (int, float)):
        raise LumaError(
            f"volume '{vol_name}' storage size must specify Kubernetes resource quantity units (e.g. 10Gi, 500Mi): {size}"
        )
    raw = str(size).strip()
    if raw.isdigit():
        raise LumaError(
            f"volume '{vol_name}' storage size must specify Kubernetes resource quantity units (e.g. 10Gi, 500Mi): {raw}"
        )
    if not QUANTITY_UNIT_RE.match(raw):
        raise LumaError(
            f"volume '{vol_name}' storage size must specify Kubernetes resource quantity units (e.g. 10Gi, 500Mi): {raw}"
        )
    return raw


def _parse_volume_entry(entry: Any) -> tuple[str, str, bool]:
    """Parse volume entry (string or mapping) into (source, target_mount_path, read_only)."""
    if isinstance(entry, str):
        parts = [p.strip() for p in entry.split(":")]
        if len(parts) == 2:
            return parts[0], parts[1], False
        elif len(parts) == 3:
            source, target, mode = parts[0], parts[1], parts[2].lower()
            if mode == "ro":
                return source, target, True
            elif mode == "rw":
                return source, target, False
            else:
                raise LumaError(f"invalid volume access mode flag {parts[2]!r} in {entry!r}")
        elif len(parts) < 2:
            raise LumaError(f"volume entry {entry!r} does not include a target mount path")
        else:
            raise LumaError(f"unsupported volume entry format: {entry!r}")
    elif isinstance(entry, dict):
        source = str(entry.get("source") or "").strip()
        target = str(entry.get("target") or "").strip()
        if not target:
            raise LumaError(f"volume mapping requires 'target': {entry}")
        read_only = bool(entry.get("read_only", False))
        return source, target, read_only
    raise LumaError(f"unsupported volume entry: {entry}")


def _resolve_service_volumes(
    *,
    project: str,
    container_name: str,
    raw_volume_entries: list[Any],
    storage_configs: Mapping[str, Any],
    existing_pod_volumes: dict[str, Dict[str, Any]],
    pvcs_by_name: dict[str, Dict[str, Any]],
    namespace: str,
    root_region: str,
    root_node: str,
) -> list[Dict[str, Any]]:
    """Resolve volume mounts for a container, validating duplicate paths and synthesizing PVCs/hostPaths."""
    container_mounts: list[Dict[str, Any]] = []
    seen_mount_paths: set[str] = set()

    for entry in raw_volume_entries:
        source, target, read_only = _parse_volume_entry(entry)
        if target in seen_mount_paths:
            raise LumaError(
                f"duplicate mount path in container '{container_name}': {target}"
            )
        seen_mount_paths.add(target)

        # Distinguish named volume vs hostPath bind
        is_named = not (source.startswith("/") or source.startswith(".") or source.startswith("~"))
        if is_named:
            vol_name = slugify(source)
            pvc_name = f"{project}-{vol_name}"
            if pvc_name not in pvcs_by_name:
                vol_spec = storage_configs.get(source) or storage_configs.get(vol_name) or {}
                if not isinstance(vol_spec, dict):
                    vol_spec = {}
                size_str = _validate_storage_size(vol_spec.get("size"), source)
                pvcs_by_name[pvc_name] = {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {
                        "name": pvc_name,
                        "namespace": namespace,
                        "annotations": {
                            "luma.io/reclaim-policy": "Retain",
                            "luma.io/region": str(vol_spec.get("region") or root_region or "cn"),
                            "luma.io/node-affinity": str(vol_spec.get("node") or root_node),
                        },
                    },
                    "spec": {
                        "accessModes": [str(vol_spec.get("accessMode") or "ReadWriteOnce")],
                        "storageClassName": str(vol_spec.get("storageClass") or "local-path"),
                        "resources": {"requests": {"storage": size_str}},
                    },
                }
            if vol_name not in existing_pod_volumes:
                existing_pod_volumes[vol_name] = {
                    "name": vol_name,
                    "persistentVolumeClaim": {"claimName": pvc_name},
                }
            mount: Dict[str, Any] = {"name": vol_name, "mountPath": target}
            if read_only:
                mount["readOnly"] = True
            container_mounts.append(mount)
        else:
            vol_name = f"hostpath-{slugify(source.lstrip('./~').replace('/', '-'))}"
            if vol_name not in existing_pod_volumes:
                existing_pod_volumes[vol_name] = {
                    "name": vol_name,
                    "hostPath": {"path": source, "type": "DirectoryOrCreate"},
                }
            mount = {"name": vol_name, "mountPath": target}
            if read_only:
                mount["readOnly"] = True
            container_mounts.append(mount)

    return container_mounts


def _compile_exposure_ingress(
    *,
    name: str,
    namespace: str,
    region: str,
    exposure: str,
    domain: str,
    port: int,
    publish_port: int | None = None,
) -> tuple[list[Dict[str, Any]], str]:
    """Compile Luma's 6 signature exposure modes into Kubernetes Ingress / Traefik IngressRouteTCP."""
    if exposure not in VALID_EXPOSURES:
        raise LumaError(f"exposure must be one of {sorted(VALID_EXPOSURES)}")
    if region:
        validate_region_exposure(region, exposure)

    if exposure == "none":
        probe = f"http://{name}.{namespace}.svc.cluster.local:{port}" if port > 0 else ""
        return [], probe

    if exposure == "tcp-relay":
        relay_port = int(publish_port or port or 0)
        if relay_port < 1:
            raise LumaError("tcp-relay requires a valid port or publishPort")
        if relay_port in TCP_RELAY_RESERVED_PORTS:
            raise LumaError(f"tcp-relay cannot use reserved Traefik port: {relay_port}")
        entrypoint = tcp_entrypoint_name(relay_port)
        probe = f"tcp://{domain or name}:{relay_port}"
        return [
            {
                "apiVersion": "traefik.io/v1alpha1",
                "kind": "IngressRouteTCP",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": {
                        "luma.io/exposure": "tcp-relay",
                        "luma.io/region": region or "cn",
                    },
                    "annotations": {
                        "luma.io/publish-port": str(relay_port),
                    },
                },
                "spec": {
                    "entryPoints": [entrypoint],
                    "routes": [
                        {
                            "match": "HostSNI(`*`)",
                            "services": [
                                {
                                    "name": name,
                                    "port": port or relay_port,
                                }
                            ],
                        }
                    ],
                },
            }
        ], probe

    if not domain:
        raise LumaError(f"exposure={exposure} requires string field: domain")
    if port < 1:
        raise LumaError(f"exposure={exposure} requires integer field: port")

    annotations: Dict[str, str] = {
        "luma.io/exposure": exposure,
        "luma.io/region": region or "cn",
        "traefik.ingress.kubernetes.io/router.entrypoints": "web,websecure",
        "traefik.ingress.kubernetes.io/router.tls": "true",
    }
    if exposure == "cn-edge":
        annotations["luma.io/edge-gateway"] = "cn-edge"
    elif exposure == "external-edge":
        annotations["luma.io/edge-gateway"] = "global-edge"
        annotations["external-dns.alpha.kubernetes.io/hostname"] = domain
    elif exposure == "tailscale-relay":
        annotations["luma.io/edge-gateway"] = "tailscale-relay"
        annotations["luma.io/relay-transport"] = "tailscale-mesh"
        annotations["luma.io/target-region"] = "home"
    elif exposure == "cloudflare-tunnel":
        annotations["luma.io/edge-gateway"] = "cloudflare-tunnel"
        annotations["luma.io/tunnel-hostname"] = domain
        annotations["luma.io/tunnel-service"] = f"http://{name}.{namespace}.svc.cluster.local:{port}"

    return [
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {
                    "luma.io/exposure": exposure,
                    "luma.io/region": region or "cn",
                },
                "annotations": annotations,
            },
            "spec": {
                "rules": [
                    {
                        "host": domain,
                        "http": {
                            "paths": [
                                {
                                    "path": "/",
                                    "pathType": "Prefix",
                                    "backend": {
                                        "service": {"name": name, "port": {"number": port}}
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
        }
    ], f"https://{domain}"


@dataclass(frozen=True)
class CompiledWorkload:
    project: str
    environment: str
    namespace: str
    artifact_id: str
    deployments: list[Dict[str, Any]]
    services: list[Dict[str, Any]]
    ingresses: list[Dict[str, Any]]
    pvcs: list[Dict[str, Any]]
    secret_manifest: Dict[str, Any]
    helm_release: Dict[str, Any]
    replicas_by_service: Dict[str, int] = field(default_factory=dict)
    sidecar_pairs: Dict[str, str] = field(default_factory=dict)

    @property
    def helm_release_manifest(self) -> Dict[str, Any]:
        """Alias for helm_release conforming to test suite and contract naming."""
        return self.helm_release

    @property
    def secrets(self) -> list[Dict[str, Any]]:
        """Return secret manifests as a list for compatibility with multi-secret or legacy accessors."""
        return [self.secret_manifest] if self.secret_manifest else []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project": self.project,
            "environment": self.environment,
            "namespace": self.namespace,
            "artifactId": self.artifact_id,
            "deployments": self.deployments,
            "services": self.services,
            "ingresses": self.ingresses,
            "pvcs": self.pvcs,
            "secretManifest": self.secret_manifest,
            "helmRelease": self.helm_release,
            "helmReleaseManifest": self.helm_release,
            "replicasByService": dict(self.replicas_by_service),
            "sidecarPairs": dict(self.sidecar_pairs),
        }


def _validate_secret_mount_path(path: str) -> str:
    raw = str(path or "").strip()
    if not raw.startswith("/") or ".." in raw.split("/") or any( ord(c) < 32 for c in raw ):
        raise LumaError(f"mounted secret file path must be a safe absolute POSIX path: {path}")
    return raw


def _build_secret_manifest(
    *,
    project_slug: str,
    namespace: str,
    secrets: Mapping[str, str] | None,
    mounted_secret_files: Mapping[str, str] | None,
) -> tuple[Dict[str, Any], list[Dict[str, Any]], list[Dict[str, Any]]]:
    """Render a K8s Secret supporting both env vars and decoded file mounts (e.g. /app/secrets/*.pem)."""
    string_data: Dict[str, str] = {}
    binary_data: Dict[str, str] = {}
    file_items: list[Dict[str, Any]] = []
    mount_dir = "/app/secrets"

    for key, val in sorted((secrets or {}).items()):
        string_data[str(key)] = str(val)

    for secret_key, target_path in sorted((mounted_secret_files or {}).items()):
        safe_target = _validate_secret_mount_path(target_path)
        raw_val = (secrets or {}).get(secret_key, "")
        try:
            decoded = base64.b64decode(raw_val.encode("ascii"), validate=True)
            b64_payload = base64.b64encode(decoded).decode("ascii")
        except Exception:
            b64_payload = base64.b64encode(str(raw_val).encode("utf-8")).decode("ascii")
        file_key = f"file__{slugify(secret_key)}"
        binary_data[file_key] = b64_payload
        filename = safe_target.rsplit("/", 1)[-1]
        parent = safe_target.rsplit("/", 1)[0] or "/app/secrets"
        mount_dir = parent
        file_items.append({"key": file_key, "path": filename, "mode": 0o400})

    secret_name = f"{project_slug}-secrets"
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": secret_name, "namespace": namespace},
        "type": "Opaque",
        "stringData": string_data,
        "data": binary_data,
    }
    volumes: list[Dict[str, Any]] = []
    volume_mounts: list[Dict[str, Any]] = []
    if file_items:
        volumes.append(
            {
                "name": "luma-secret-files",
                "secret": {
                    "secretName": secret_name,
                    "items": file_items,
                    "defaultMode": 0o400,
                },
            }
        )
        volume_mounts.append(
            {
                "name": "luma-secret-files",
                "mountPath": mount_dir,
                "readOnly": True,
            }
        )
    return manifest, volumes, volume_mounts


def compile_flux_helm_release(
    *,
    project: str,
    environment: str,
    namespace: str,
    generation: int,
    artifact: BuildArtifact,
    values: Mapping[str, Any],
    gateway_probe_url: str = "",
    reconcile_requested_at: str = "",
    annotations: Mapping[str, str] | None = None,
) -> Dict[str, Any]:
    """Render a Flux HelmRelease with automatic rollback on upgrade or route probe failure."""
    project_slug = slugify(project)
    req_at = reconcile_requested_at or datetime.now(timezone.utc).isoformat()
    helm_annotations: Dict[str, str] = {
        "reconcile.fluxcd.io/requestedAt": req_at,
        **(dict(annotations) if annotations else {}),
    }
    return {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {
            "name": project_slug,
            "namespace": namespace,
            "labels": {
                "luma.io/project": project_slug,
                "luma.io/environment": environment,
                "luma.io/artifact-id": artifact.artifact_id,
                "luma.io/generation": str(generation),
            },
            "annotations": helm_annotations,
        },
        "spec": {
            "interval": "5m",
            "timeout": "2m",
            "chartRef": {
                "kind": "OCIRepository",
                "name": "luma-workload-chart",
                "namespace": "luma-system",
            },
            "install": {
                "remediation": {
                    "retries": 1,
                }
            },
            "upgrade": {
                "cleanupOnFail": True,
                "remediation": {
                    "strategy": "rollback",
                    "retries": 1,
                },
            },
            "test": {
                "enable": bool(gateway_probe_url),
                "ignoreFailures": False,
            },
            "driftDetection": {
                "mode": "enabled",
                "ignore": [
                    {
                        "paths": ["/spec/replicas"],
                        "target": {"kind": "Deployment"},
                    }
                ],
            },
            "values": {
                **dict(values),
                "gatewayProbeUrl": gateway_probe_url,
            },
        },
    }


def compile_service_release(
    artifact: BuildArtifact,
    *,
    environment: str = "prod",
    generation: int = 1,
    replicas_override: int | Mapping[str, int] | None = None,
    secrets: Mapping[str, str] | None = None,
    mounted_secret_files: Mapping[str, str] | None = None,
    node_architectures: Mapping[str, str] | None = None,
) -> CompiledWorkload:
    """Compile a single-service luma.yaml manifest + BuildArtifact into K8s + Flux resources."""
    parsed = yaml.safe_load(artifact.manifest_snapshot) or {}
    if not isinstance(parsed, dict):
        raise LumaError("artifact manifest_snapshot must be a YAML mapping")
    project = slugify(str(parsed.get("name") or artifact.project))
    namespace = f"luma-{environment}"
    image_art = next(iter(artifact.images.values()))
    if isinstance(replicas_override, Mapping):
        single_replicas = replicas_override.get(project)
        if single_replicas is None and len(replicas_override) == 1:
            single_replicas = next(iter(replicas_override.values()))
    else:
        single_replicas = replicas_override
    replicas = int(single_replicas if single_replicas is not None else (parsed.get("replicas") or 1))
    port = int(parsed.get("port") or 3000)
    publish_port = int(parsed["publishPort"]) if parsed.get("publishPort") is not None else None
    domain = str(parsed.get("domain") or "").strip()
    raw_region = str(parsed.get("region") or "").strip()
    region = parse_region_name(raw_region) if raw_region else ""
    if parsed.get("exposure") is not None:
        exposure = str(parsed.get("exposure")).strip()
    else:
        exposure = "cn-edge" if domain and region == "cn" else "external-edge" if domain and region == "global" else "none"
    node_pin = str(parsed.get("node") or "").strip()
    proxy_flag = parsed.get("proxy") if "proxy" in parsed else None

    image_platform = getattr(image_art, "platform", "") or ""
    if not image_platform:
        image_platform = str(parsed.get("platform") or "")
    if not image_platform:
        image_platform = infer_native_platform(
            platform="",
            target_node=node_pin,
            target_region=region,
            node_architectures=node_architectures,
        )

    arch = _validate_workload_architecture(
        image_platform,
        region=region,
        node_pin=node_pin,
        node_architectures=node_architectures,
        service_name=project,
    )

    secret_manifest, secret_vols, secret_mounts = _build_secret_manifest(
        project_slug=project,
        namespace=namespace,
        secrets=secrets,
        mounted_secret_files=mounted_secret_files,
    )

    pod_volumes_by_name: dict[str, Dict[str, Any]] = {
        v["name"]: dict(v) for v in secret_vols
    }
    pvcs_by_name: dict[str, Dict[str, Any]] = {}
    storage_cfg = parsed.get("storage") if isinstance(parsed.get("storage"), dict) else {}
    for vol_k, vol_v in storage_cfg.items():
        if isinstance(vol_v, dict) and "size" in vol_v:
            _validate_storage_size(vol_v["size"], vol_k)

    service_vol_entries = list(parsed.get("volumes") or [])
    service_mounts = _resolve_service_volumes(
        project=project,
        container_name=project,
        raw_volume_entries=service_vol_entries,
        storage_configs=storage_cfg,
        existing_pod_volumes=pod_volumes_by_name,
        pvcs_by_name=pvcs_by_name,
        namespace=namespace,
        root_region=region or "cn",
        root_node=node_pin,
    )

    for vol_k, vol_v in sorted(storage_cfg.items()):
        pvc_name = f"{project}-{slugify(vol_k)}"
        if pvc_name not in pvcs_by_name:
            size_str = _validate_storage_size(vol_v.get("size") if isinstance(vol_v, dict) else None, vol_k)
            pvcs_by_name[pvc_name] = {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {
                    "name": pvc_name,
                    "namespace": namespace,
                    "annotations": {
                        "luma.io/reclaim-policy": "Retain",
                        "luma.io/region": region or "cn",
                        "luma.io/node-affinity": node_pin,
                    },
                },
                "spec": {
                    "accessModes": [str((vol_v.get("accessMode") if isinstance(vol_v, dict) else None) or "ReadWriteOnce")],
                    "storageClassName": str((vol_v.get("storageClass") if isinstance(vol_v, dict) else None) or "local-path"),
                    "resources": {"requests": {"storage": size_str}},
                },
            }

    pvcs = list(pvcs_by_name.values())
    pod_vols = list(pod_volumes_by_name.values())
    container_mounts = list(secret_mounts) + service_mounts

    proxy_env, egress_mode = _build_proxy_env(region=region, proxy_enabled=proxy_flag)
    container_spec: Dict[str, Any] = {
        "name": project,
        "image": image_art.immutable_ref,
        "ports": [{"containerPort": port}],
        "envFrom": [{"secretRef": {"name": secret_manifest["metadata"]["name"]}}],
        "volumeMounts": container_mounts,
    }
    if proxy_env:
        container_spec["env"] = proxy_env
    if parsed.get("command"):
        cmd = parsed["command"]
        container_spec["command"] = cmd if isinstance(cmd, list) else ["sh", "-c", str(cmd)]

    pod_spec: Dict[str, Any] = {
        "containers": [container_spec],
        "volumes": pod_vols,
        "securityContext": {"fsGroup": 1000},
    }
    node_selector = _build_region_node_selector(region, node_pin, arch=arch)
    if node_selector:
        pod_spec["nodeSelector"] = node_selector

    labels: Dict[str, str] = {
        "app.kubernetes.io/name": project,
        "luma.io/artifact-id": artifact.artifact_id,
        "luma.io/generation": str(generation),
        "luma.io/exposure": exposure,
        "luma.io/egress": egress_mode,
    }
    if region:
        labels["luma.io/region"] = region

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": project,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app.kubernetes.io/name": project}},
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": project,
                        **({"luma.io/region": region} if region else {}),
                        "luma.io/exposure": exposure,
                    },
                    "annotations": {
                        "luma.io/egress-mode": egress_mode,
                    },
                },
                "spec": pod_spec,
            },
        },
    }

    svc_port = port or int(publish_port or 0)
    port_name = "tcp" if exposure == "tcp-relay" else "http"
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": project, "namespace": namespace},
        "spec": {
            "selector": {"app.kubernetes.io/name": project},
            "ports": [{"name": port_name, "port": svc_port, "targetPort": port or svc_port}],
        },
    }

    ingresses, probe_url = _compile_exposure_ingress(
        name=project,
        namespace=namespace,
        region=region,
        exposure=exposure,
        domain=domain,
        port=port,
        publish_port=publish_port,
    )

    helm_release = compile_flux_helm_release(
        project=project,
        environment=environment,
        namespace=namespace,
        generation=generation,
        artifact=artifact,
        values={
            "project": project,
            "artifactId": artifact.artifact_id,
            "region": region,
            "exposure": exposure,
            "egress": egress_mode,
            "services": {
                project: {
                    "image": image_art.immutable_ref,
                    "replicas": replicas,
                    "port": port,
                    "domain": domain,
                    "region": region,
                    "exposure": exposure,
                    "proxy": bool(proxy_env),
                }
            },
        },
        gateway_probe_url=probe_url,
    )

    return CompiledWorkload(
        project=project,
        environment=environment,
        namespace=namespace,
        artifact_id=artifact.artifact_id,
        deployments=[deployment],
        services=[service],
        ingresses=ingresses,
        pvcs=pvcs,
        secret_manifest=secret_manifest,
        helm_release=helm_release,
        replicas_by_service={project: replicas},
    )


def compile_compose_release(
    artifact: BuildArtifact,
    *,
    environment: str = "prod",
    generation: int = 1,
    replicas_override: Mapping[str, int] | None = None,
    secrets: Mapping[str, str] | None = None,
    mounted_secret_files: Mapping[str, str] | None = None,
    node_architectures: Mapping[str, str] | None = None,
) -> CompiledWorkload:
    """Compile a multi-service Compose stack into independent K8s Deployments + Sidecars.

    Unlike legacy Nomad allocations that forced Web, Browser Worker, and Video Worker
    into a single Fate-shared allocation, each independent Compose service compiles
    into its own Kubernetes Deployment so workers can scale and restart independently.
    Containers that explicitly share network_mode: service:<primary> are attached as
    native Pod Sidecars. Preserves Luma's region, 6 exposure modes, and proxy: true.
    """
    sidecar_cfg = yaml.safe_load(artifact.manifest_snapshot) or {}
    compose_cfg = yaml.safe_load(artifact.compose_snapshot) or {}
    if not isinstance(sidecar_cfg, dict) or not isinstance(compose_cfg, dict):
        raise LumaError("compose artifact requires valid manifest_snapshot and compose_snapshot")

    project = slugify(str(sidecar_cfg.get("name") or artifact.project))
    namespace = f"luma-{environment}"
    compose_services = compose_cfg.get("services") or {}
    if not isinstance(compose_services, dict) or not compose_services:
        raise LumaError("compose_snapshot requires non-empty services")
    luma_services = sidecar_cfg.get("services") if isinstance(sidecar_cfg.get("services"), dict) else {}
    root_node = str(sidecar_cfg.get("node") or "").strip()
    raw_root_region = str(sidecar_cfg.get("region") or "").strip()
    root_region = parse_region_name(raw_root_region) if raw_root_region else ""
    root_proxy = sidecar_cfg.get("proxy") if "proxy" in sidecar_cfg else None

    secret_manifest, secret_vols, secret_mounts = _build_secret_manifest(
        project_slug=project,
        namespace=namespace,
        secrets=secrets,
        mounted_secret_files=mounted_secret_files,
    )

    sidecar_pairs: Dict[str, str] = {}
    for svc_name, svc_body in compose_services.items():
        if not isinstance(svc_body, dict):
            continue
        net_mode = str(svc_body.get("network_mode") or "").strip()
        if net_mode.startswith("service:"):
            target_svc = net_mode.split(":", 1)[1].strip()
            if not target_svc or target_svc not in compose_services:
                raise LumaError(
                    f"sidecar target service {target_svc!r} does not exist in compose file (missing parent service)"
                )
            if target_svc == svc_name:
                raise LumaError(
                    f"service {svc_name!r} cannot declare self-referential network_mode: service:{target_svc} (loop)"
                )
            sidecar_pairs[str(svc_name)] = target_svc

    # Check for cycles and resolve root primary containers (handles chained sidecars A -> B -> C)
    def _find_root_primary(s: str, visited: list[str]) -> str:
        parent = sidecar_pairs.get(s)
        if parent:
            if parent in visited:
                raise LumaError(
                    f"cyclic network_mode dependency loop detected: {' -> '.join(visited + [parent])}"
                )
            return _find_root_primary(parent, visited + [parent])
        return s

    root_primary_map: Dict[str, str] = {}
    for child in sidecar_pairs:
        root_primary_map[child] = _find_root_primary(child, [child])

    # Validate sidecar architecture parity against root primary service
    for child_name, root_parent in sorted(root_primary_map.items()):
        child_body = compose_services[child_name]
        parent_body = compose_services[root_parent]

        child_platform = (
            artifact.images[child_name].platform
            if child_name in artifact.images and getattr(artifact.images[child_name], "platform", "")
            else str(child_body.get("platform") or "")
        )
        parent_platform = (
            artifact.images[root_parent].platform
            if root_parent in artifact.images and getattr(artifact.images[root_parent], "platform", "")
            else str(parent_body.get("platform") or "")
        )

        child_luma = luma_services.get(child_name) if isinstance(luma_services.get(child_name), dict) else {}
        parent_luma = luma_services.get(root_parent) if isinstance(luma_services.get(root_parent), dict) else {}

        child_node = str(child_luma.get("node") or root_node).strip()
        parent_node = str(parent_luma.get("node") or root_node).strip()

        child_raw_reg = str(child_luma.get("region") or root_region).strip()
        child_region = parse_region_name(child_raw_reg) if child_raw_reg else ""
        parent_raw_reg = str(parent_luma.get("region") or root_region).strip()
        parent_region = parse_region_name(parent_raw_reg) if parent_raw_reg else ""

        if not child_platform:
            child_platform = infer_native_platform(
                platform="",
                target_node=child_node,
                target_region=child_region,
                node_architectures=node_architectures,
            )
        if not parent_platform:
            parent_platform = infer_native_platform(
                platform="",
                target_node=parent_node,
                target_region=parent_region,
                node_architectures=node_architectures,
            )

        child_norm = validate_native_platform(child_platform)
        parent_norm = validate_native_platform(parent_platform)
        child_arch = _extract_kubernetes_arch(child_norm)
        parent_arch = _extract_kubernetes_arch(parent_norm)

        if child_arch != parent_arch:
            raise LumaError(
                f"cannot co-locate mixed-architecture containers in pod sidecar: "
                f"{child_name} ({child_norm}) vs {root_parent} ({parent_norm})"
            )

    deployments: list[Dict[str, Any]] = []
    k8s_services: list[Dict[str, Any]] = []
    ingresses: list[Dict[str, Any]] = []
    replicas_by_service: Dict[str, int] = {}
    helm_services_values: Dict[str, Any] = {}
    primary_probe_url = ""

    combined_storage: Dict[str, Any] = {}
    for source_dict in [sidecar_cfg.get("storage"), sidecar_cfg.get("volumes"), compose_cfg.get("volumes")]:
        if isinstance(source_dict, dict):
            for k, v in source_dict.items():
                if isinstance(v, dict):
                    combined_storage[str(k)] = v
                else:
                    combined_storage[str(k)] = {}

    for vol_k, vol_v in combined_storage.items():
        if isinstance(vol_v, dict) and "size" in vol_v:
            _validate_storage_size(vol_v["size"], vol_k)

    pvcs_by_name: dict[str, Dict[str, Any]] = {}
    seen_tcp_ports: set[int] = set()

    for svc_name, svc_body in sorted(compose_services.items()):
        if svc_name in root_primary_map or not isinstance(svc_body, dict):
            continue
        luma_svc = luma_services.get(svc_name) if isinstance(luma_services.get(svc_name), dict) else {}
        default_replicas = int(luma_svc.get("replicas") or (svc_body.get("deploy") or {}).get("replicas") or 1)
        replicas = int(
            replicas_override[svc_name]
            if replicas_override and svc_name in replicas_override
            else default_replicas
        )
        replicas_by_service[svc_name] = replicas

        if svc_name in artifact.images:
            image_ref = artifact.images[svc_name].immutable_ref
        else:
            image_ref = str(svc_body.get("image") or "")
        if not image_ref:
            raise LumaError(f"compose service {svc_name} has no resolved immutable image")

        raw_svc_region = str(luma_svc.get("region") or root_region).strip()
        svc_region = parse_region_name(raw_svc_region) if raw_svc_region else ""
        node_pin = str(luma_svc.get("node") or root_node).strip()

        svc_platform = (
            artifact.images[svc_name].platform
            if svc_name in artifact.images and getattr(artifact.images[svc_name], "platform", "")
            else str(svc_body.get("platform") or "")
        )
        if not svc_platform:
            svc_platform = infer_native_platform(
                platform="",
                target_node=node_pin,
                target_region=svc_region,
                node_architectures=node_architectures,
            )

        svc_arch = _validate_workload_architecture(
            svc_platform,
            region=svc_region,
            node_pin=node_pin,
            node_architectures=node_architectures,
            service_name=svc_name,
        )

        svc_proxy_flag = luma_svc.get("proxy") if "proxy" in luma_svc else root_proxy
        proxy_env, egress_mode = _build_proxy_env(region=svc_region, proxy_enabled=svc_proxy_flag)

        port = int(luma_svc.get("port") or 0)
        publish_port = int(luma_svc["publishPort"]) if luma_svc.get("publishPort") is not None else None
        domain = str(luma_svc.get("domain") or "").strip()
        if luma_svc.get("exposure") is not None:
            exposure = str(luma_svc.get("exposure")).strip()
        else:
            exposure = (
                "cn-edge" if domain and (svc_region or "cn") == "cn"
                else "external-edge" if domain and svc_region == "global"
                else "none"
            )

        if exposure == "tcp-relay":
            relay_port = int(publish_port or port or 0)
            if relay_port in seen_tcp_ports:
                raise LumaError(
                    f"TCP relay port collision: publishPort {relay_port} already in use by another service in this release"
                )
            seen_tcp_ports.add(relay_port)

        pod_volumes_by_name: dict[str, Dict[str, Any]] = {
            v["name"]: dict(v) for v in secret_vols
        }

        # Resolve volumes for main container
        main_vol_entries: list[Any] = []
        seen_raw: set[str] = set()
        for e in list(luma_svc.get("volumes") or []) + list(svc_body.get("volumes") or []):
            key = str(e)
            if key not in seen_raw:
                seen_raw.add(key)
                main_vol_entries.append(e)

        main_mounts = _resolve_service_volumes(
            project=project,
            container_name=svc_name,
            raw_volume_entries=main_vol_entries,
            storage_configs=combined_storage,
            existing_pod_volumes=pod_volumes_by_name,
            pvcs_by_name=pvcs_by_name,
            namespace=namespace,
            root_region=svc_region or root_region or "cn",
            root_node=node_pin or root_node,
        )

        main_container: Dict[str, Any] = {
            "name": slugify(svc_name),
            "image": image_ref,
            "envFrom": [{"secretRef": {"name": secret_manifest["metadata"]["name"]}}],
            "volumeMounts": list(secret_mounts) + main_mounts,
        }
        container_port = port or int(publish_port or 0)
        if container_port > 0:
            port_name = "tcp" if exposure == "tcp-relay" else "http"
            main_container["ports"] = [{"name": port_name, "containerPort": container_port}]
        if proxy_env:
            main_container["env"] = proxy_env
        if svc_body.get("command"):
            cmd = svc_body["command"]
            main_container["command"] = cmd if isinstance(cmd, list) else ["sh", "-c", str(cmd)]

        containers: list[Dict[str, Any]] = [main_container]

        for child_name, root_parent in sorted(root_primary_map.items()):
            if root_parent != svc_name:
                continue
            child_body = compose_services[child_name]
            child_img = (
                artifact.images[child_name].immutable_ref
                if child_name in artifact.images
                else str(child_body.get("image") or "")
            )
            child_luma = luma_services.get(child_name) if isinstance(luma_services.get(child_name), dict) else {}
            child_vol_entries: list[Any] = []
            child_seen_raw: set[str] = set()
            for e in list(child_luma.get("volumes") or []) + list(child_body.get("volumes") or []):
                key = str(e)
                if key not in child_seen_raw:
                    child_seen_raw.add(key)
                    child_vol_entries.append(e)

            child_mounts = _resolve_service_volumes(
                project=project,
                container_name=child_name,
                raw_volume_entries=child_vol_entries,
                storage_configs=combined_storage,
                existing_pod_volumes=pod_volumes_by_name,
                pvcs_by_name=pvcs_by_name,
                namespace=namespace,
                root_region=svc_region or root_region or "cn",
                root_node=node_pin or root_node,
            )
            child_container: Dict[str, Any] = {
                "name": slugify(child_name),
                "image": child_img,
                "envFrom": [{"secretRef": {"name": secret_manifest["metadata"]["name"]}}],
                "volumeMounts": list(secret_mounts) + child_mounts,
            }
            if proxy_env:
                child_container["env"] = proxy_env
            if child_body.get("command"):
                child_cmd = child_body["command"]
                child_container["command"] = child_cmd if isinstance(child_cmd, list) else ["sh", "-c", str(child_cmd)]
            containers.append(child_container)

        deploy_name = f"{project}-{slugify(svc_name)}"
        pod_spec: Dict[str, Any] = {
            "containers": containers,
            "volumes": list(pod_volumes_by_name.values()),
            "securityContext": {"fsGroup": int(luma_svc.get("fsGroup") or 1000)},
        }
        node_selector = _build_region_node_selector(svc_region, node_pin, arch=svc_arch)
        if node_selector:
            pod_spec["nodeSelector"] = node_selector

        deploy_labels: Dict[str, str] = {
            "app.kubernetes.io/name": deploy_name,
            "app.kubernetes.io/part-of": project,
            "luma.io/service": svc_name,
            "luma.io/artifact-id": artifact.artifact_id,
            "luma.io/generation": str(generation),
            "luma.io/exposure": exposure,
            "luma.io/egress": egress_mode,
        }
        if svc_region:
            deploy_labels["luma.io/region"] = svc_region

        deployments.append(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "name": deploy_name,
                    "namespace": namespace,
                    "labels": deploy_labels,
                },
                "spec": {
                    "replicas": replicas,
                    "selector": {"matchLabels": {"app.kubernetes.io/name": deploy_name}},
                    "template": {
                        "metadata": {
                            "labels": {
                                "app.kubernetes.io/name": deploy_name,
                                "app.kubernetes.io/part-of": project,
                                "luma.io/service": svc_name,
                                **({"luma.io/region": svc_region} if svc_region else {}),
                                "luma.io/exposure": exposure,
                            },
                            "annotations": {
                                "luma.io/egress-mode": egress_mode,
                            },
                        },
                        "spec": pod_spec,
                    },
                },
            }
        )

        if port > 0 or exposure == "tcp-relay":
            svc_port = port or int(publish_port or 0)
            port_name = "tcp" if exposure == "tcp-relay" else "http"
            k8s_services.append(
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": deploy_name, "namespace": namespace},
                    "spec": {
                        "selector": {"app.kubernetes.io/name": deploy_name},
                        "ports": [{"name": port_name, "port": svc_port, "targetPort": svc_port}],
                    },
                }
            )
            if not primary_probe_url and svc_port > 0:
                primary_probe_url = f"http://{deploy_name}.{namespace}.svc.cluster.local:{svc_port}"

        if exposure != "none":
            svc_ingresses, svc_probe_url = _compile_exposure_ingress(
                name=deploy_name,
                namespace=namespace,
                region=svc_region,
                exposure=exposure,
                domain=domain,
                port=port,
                publish_port=publish_port,
            )
            ingresses.extend(svc_ingresses)
            if svc_probe_url:
                primary_probe_url = svc_probe_url
        else:
            if exposure not in VALID_EXPOSURES:
                raise LumaError(f"exposure must be one of {sorted(VALID_EXPOSURES)}")
            if svc_region:
                validate_region_exposure(svc_region, exposure)

        helm_services_values[svc_name] = {
            "deploymentName": deploy_name,
            "image": image_ref,
            "replicas": replicas,
            "region": svc_region,
            "node": node_pin,
            "exposure": exposure,
            "egress": egress_mode,
            "proxy": bool(proxy_env),
            "port": port,
            "domain": domain,
        }

    for vol_k, vol_v in sorted(combined_storage.items()):
        pvc_name = f"{project}-{slugify(vol_k)}"
        if pvc_name not in pvcs_by_name:
            size_str = _validate_storage_size(vol_v.get("size") if isinstance(vol_v, dict) else None, vol_k)
            pvcs_by_name[pvc_name] = {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {
                    "name": pvc_name,
                    "namespace": namespace,
                    "annotations": {
                        "luma.io/reclaim-policy": "Retain",
                        "luma.io/region": str((vol_v.get("region") if isinstance(vol_v, dict) else None) or root_region or "cn"),
                        "luma.io/node-affinity": str((vol_v.get("node") if isinstance(vol_v, dict) else None) or root_node),
                    },
                },
                "spec": {
                    "accessModes": [str((vol_v.get("accessMode") if isinstance(vol_v, dict) else None) or "ReadWriteOnce")],
                    "storageClassName": str((vol_v.get("storageClass") if isinstance(vol_v, dict) else None) or "local-path"),
                    "resources": {"requests": {"storage": size_str}},
                },
            }

    pvcs = list(pvcs_by_name.values())

    helm_release = compile_flux_helm_release(
        project=project,
        environment=environment,
        namespace=namespace,
        generation=generation,
        artifact=artifact,
        values={
            "project": project,
            "artifactId": artifact.artifact_id,
            "region": root_region,
            "services": helm_services_values,
        },
        gateway_probe_url=primary_probe_url,
    )

    return CompiledWorkload(
        project=project,
        environment=environment,
        namespace=namespace,
        artifact_id=artifact.artifact_id,
        deployments=deployments,
        services=k8s_services,
        ingresses=ingresses,
        pvcs=pvcs,
        secret_manifest=secret_manifest,
        helm_release=helm_release,
        replicas_by_service=replicas_by_service,
        sidecar_pairs=sidecar_pairs,
    )

