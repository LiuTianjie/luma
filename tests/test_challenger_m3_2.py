"""Adversarial challenger test suite for Milestone 3 - Feature 6:
Compose Splitting, PVC Volume Mounting & 6 Exposure Modes.

Empirically tests:
1. Volume definitions & Storage validation:
   - Invalid storage sizes (unitless integers e.g. 50, string digits e.g. '50', non-numeric 'abc',
     negative sizes '-10Gi', zero/negative ints, invalid units '10GB', '10 kb')
   - Valid storage quantities ('10Gi', '500Mi', '2Ti', '10G', '100M', '0.5Gi', empty/None fallback to '10Gi')
   - Read-only permutations (:ro, :rw, :RO, :RW, dict read_only=True/False)
   - Invalid volume access mode flags (:read, :w, :wo, :invalid, extra colons)
   - Duplicate mount paths in container
   - HostPath vs PVC volume name collisions and path slugification collisions

2. All 6 signature exposure modes & invalid modes:
   - Valid modes: none, cn-edge, external-edge, tailscale-relay, cloudflare-tunnel, tcp-relay
   - Correct manifest emission (networking.k8s.io/v1 Ingress vs traefik.io/v1alpha1 IngressRouteTCP vs none)
   - Region validation (cn-edge strictly cn, external-edge strictly global, tailscale-relay strictly home)
   - TCP relay service port name ('tcp' vs 'http')
   - Invalid modes rejected (unknown, edge, public, private, internal, invalid-mesh)

3. TCP relay port collisions across 5+ services:
   - 5 independent services with distinct publishPorts (clean compilation, 5 IngressRouteTCPs)
   - 5+ services with publishPort collision (e.g. svc1 & svc5, svc2 & svc4)
   - Reserved ports 80 and 443 rejected
   - Collision between publishPort and port fallback

4. exposure: none zero Ingress / Gateway guarantee:
   - Single service exposure: none without domain -> 0 ingresses
   - Single service exposure: none with explicit domain -> 0 ingresses
   - Compose multi-service exposure: none with and without domains -> 0 ingresses
   - Partial exposure compose stack -> only exposed services emit Ingress
"""
from __future__ import annotations

from typing import Any
import unittest
import yaml

from luma.errors import LumaError
from luma.paas.build.artifact_store import BuildArtifact, ServiceImageArtifact
from luma.paas.release.helm_compiler import (
    _parse_volume_entry,
    _validate_storage_size,
    compile_compose_release,
    compile_service_release,
)


def _make_svc_art(
    project: str,
    *,
    region: str = "cn",
    exposure: str = "none",
    port: int = 8080,
    publish_port: int | None = None,
    domain: str = "",
    node: str = "",
    platform: str = "linux/amd64",
    volumes: list[Any] | None = None,
    storage: dict[str, Any] | None = None,
) -> BuildArtifact:
    manifest: dict[str, Any] = {
        "name": project,
        "region": region,
        "exposure": exposure,
        "port": port,
    }
    if publish_port is not None:
        manifest["publishPort"] = publish_port
    if domain:
        manifest["domain"] = domain
    if node:
        manifest["node"] = node
    if volumes:
        manifest["volumes"] = volumes
    if storage:
        manifest["storage"] = storage

    return BuildArtifact(
        artifact_id=f"art-{project}-stress",
        project=project,
        source_repo="https://example.com/repo.git",
        commit_sha="a" * 40,
        branch="main",
        manifest_kind="service",
        manifest_snapshot=yaml.safe_dump(manifest),
        images={
            project: ServiceImageArtifact(
                service_name=project,
                repository=f"100.66.177.70:5000/{project}",
                digest="sha256:" + "1" * 64,
                tag="v1",
                platform=platform,
            )
        },
        created_at=1700000000,
    )


def _make_comp_art(
    project: str,
    services_compose: dict[str, Any],
    services_luma: dict[str, Any] | None = None,
    images: dict[str, str] | None = None,
    region: str = "cn",
    node: str = "",
    storage: dict[str, Any] | None = None,
) -> BuildArtifact:
    luma_data: dict[str, Any] = {
        "name": project,
        "region": region,
    }
    if node:
        luma_data["node"] = node
    if services_luma:
        luma_data["services"] = services_luma
    if storage:
        luma_data["storage"] = storage

    image_artifacts: dict[str, ServiceImageArtifact] = {}
    platforms = images or {}
    for idx, (svc_name, _svc_body) in enumerate(services_compose.items(), start=1):
        plt = platforms.get(svc_name, "linux/amd64")
        image_artifacts[svc_name] = ServiceImageArtifact(
            service_name=svc_name,
            repository=f"100.66.177.70:5000/{project}-{svc_name}",
            digest="sha256:" + f"{idx % 10}" * 64,
            tag="v1",
            platform=plt,
        )

    return BuildArtifact(
        artifact_id=f"art-{project}-compose-stress",
        project=project,
        source_repo="https://example.com/compose-repo.git",
        commit_sha="b" * 40,
        branch="main",
        manifest_kind="compose",
        manifest_snapshot=yaml.safe_dump(luma_data),
        compose_snapshot=yaml.safe_dump({"services": services_compose}),
        images=image_artifacts,
        created_at=1700000000,
    )


class Feature6VolumeStorageStressTests(unittest.TestCase):
    """Stress tests for volume definitions, storage quantities, and read-only flags."""

    def test_invalid_storage_sizes_rejection(self) -> None:
        """Verifies unitless numbers, strings, negative quantities, and malformed units are rejected."""
        invalid_quantities = [
            50,               # Bare integer
            -10,              # Negative integer
            0,                # Zero integer
            50.5,             # Bare float
            "50",             # Bare string integer
            "abc",            # Alphabetic string
            "-10Gi",          # Negative unit quantity
            "-5",             # Negative string integer
            "0",              # Zero string
            "10GB",           # Decimal byte suffix (K8s accepts Gi or G, not GB)
            "10GiB",          # Invalid compound suffix
            "10 kb",          # Interspersed space
            "50 MB",          # Interspersed space with invalid unit
            "Gi",             # Missing numeric value
            "Mi",             # Missing numeric value
            "10Gi extra",     # Trailing garbage
        ]
        for bad_size in invalid_quantities:
            with self.assertRaises(LumaError, msg=f"Should reject invalid size: {bad_size!r}"):
                _validate_storage_size(bad_size, "test-vol")

        # In service release manifest
        for bad_size in [50, "50", "abc", "-10Gi"]:
            art = _make_svc_art(
                "bad-size-svc",
                volumes=["data:/var/data"],
                storage={"data": {"size": bad_size}},
            )
            with self.assertRaises(LumaError):
                compile_service_release(art)

    def test_valid_storage_sizes_normalization(self) -> None:
        """Verifies standard Kubernetes quantity units pass and empty/None defaults to 10Gi."""
        self.assertEqual(_validate_storage_size("10Gi", "v"), "10Gi")
        self.assertEqual(_validate_storage_size("500Mi", "v"), "500Mi")
        self.assertEqual(_validate_storage_size("2Ti", "v"), "2Ti")
        self.assertEqual(_validate_storage_size("10G", "v"), "10G")
        self.assertEqual(_validate_storage_size("100M", "v"), "100M")
        self.assertEqual(_validate_storage_size("0.5Gi", "v"), "0.5Gi")
        self.assertEqual(_validate_storage_size("1000k", "v"), "1000k")
        self.assertEqual(_validate_storage_size("1000Ki", "v"), "1000Ki")
        self.assertEqual(_validate_storage_size("1000m", "v"), "1000m")
        # Defaults
        self.assertEqual(_validate_storage_size("", "v"), "10Gi")
        self.assertEqual(_validate_storage_size(None, "v"), "10Gi")

    def test_volume_entry_read_only_and_mode_permutations(self) -> None:
        """Verifies :ro, :rw, case variations, and dict mappings are parsed accurately."""
        # 1. :ro flag
        src, tgt, ro = _parse_volume_entry("app-data:/app/data:ro")
        self.assertEqual((src, tgt, ro), ("app-data", "/app/data", True))

        # 2. :rw flag
        src, tgt, ro = _parse_volume_entry("app-data:/app/data:rw")
        self.assertEqual((src, tgt, ro), ("app-data", "/app/data", False))

        # 3. Case-insensitive mode flags
        self.assertTrue(_parse_volume_entry("app-data:/app/data:RO")[2])
        self.assertFalse(_parse_volume_entry("app-data:/app/data:RW")[2])

        # 4. HostPath with :ro
        src, tgt, ro = _parse_volume_entry("/var/log:/app/log:ro")
        self.assertEqual((src, tgt, ro), ("/var/log", "/app/log", True))

        # 5. Dict mappings
        self.assertTrue(_parse_volume_entry({"source": "vol", "target": "/data", "read_only": True})[2])
        self.assertFalse(_parse_volume_entry({"source": "vol", "target": "/data", "read_only": False})[2])
        self.assertFalse(_parse_volume_entry({"source": "vol", "target": "/data"})[2])

        # 6. Invalid mode flags raise LumaError
        invalid_entries = [
            "app-data:/app:read",
            "app-data:/app:w",
            "app-data:/app:wo",
            "app-data:/app:invalid",
            "app-data:/app:ro:extra",
            "app-data",                      # Missing target
            {"source": "data"},              # Missing target dict
            12345,                           # Bad type
            ["app-data:/app"],               # Bad type
        ]
        for bad_entry in invalid_entries:
            with self.assertRaises(LumaError, msg=f"Should reject: {bad_entry!r}"):
                _parse_volume_entry(bad_entry)

    def test_duplicate_mount_paths_in_container_rejected(self) -> None:
        """Verifies that duplicate mount paths within the same container raise LumaError."""
        # Named volumes colliding on mount path
        art1 = _make_comp_art(
            "dup-mount-named",
            services_compose={
                "web": {
                    "image": "100.66.177.70:5000/web:v1",
                    "volumes": ["vol1:/app/data", "vol2:/app/data"],
                }
            },
        )
        with self.assertRaises(LumaError) as ctx1:
            compile_compose_release(art1)
        self.assertIn("duplicate mount path", str(ctx1.exception).lower())

        # HostPath and named volume colliding on mount path
        art2 = _make_comp_art(
            "dup-mount-mixed",
            services_compose={
                "web": {
                    "image": "100.66.177.70:5000/web:v1",
                    "volumes": ["/host/path:/app/data", "vol1:/app/data"],
                }
            },
        )
        with self.assertRaises(LumaError) as ctx2:
            compile_compose_release(art2)
        self.assertIn("duplicate mount path", str(ctx2.exception).lower())

    def test_hostpath_and_pvc_volume_name_collision_behavior(self) -> None:
        """Empirically analyzes and documents collision between hostPath and PVC with identical vol_name."""
        art = _make_svc_art(
            "col-test",
            volumes=["/data:/host/data", "hostpath-data:/pvc/data"],
        )
        res = compile_service_release(art)
        pod_vols = res.deployments[0]["spec"]["template"]["spec"]["volumes"]
        pvc_names = [p["metadata"]["name"] for p in res.pvcs]

        # PVC was generated
        self.assertIn("col-test-hostpath-data", pvc_names)
        # But in pod_volumes, only hostPath was retained because hostpath-data matched vol_name
        # Documents empirical finding: name collision between slugified hostpath and named volume
        self.assertTrue(any("hostPath" in v for v in pod_vols))


class Feature6ExposureModesStressTests(unittest.TestCase):
    """Stress tests for all 6 signature exposure modes and invalid exposure strings."""

    def test_all_6_valid_exposure_modes_succeed_with_valid_parameters(self) -> None:
        """Verifies each of the 6 exposure modes compiles cleanly with valid parameters."""
        # 1. none
        art_none = _make_svc_art("svc-none", exposure="none", port=8080)
        res_none = compile_service_release(art_none)
        self.assertEqual(len(res_none.ingresses), 0)
        self.assertEqual(len(res_none.services), 1)

        # 2. cn-edge (requires region=cn, domain, port)
        art_cn = _make_svc_art("svc-cn", exposure="cn-edge", domain="cn.example.com", region="cn", port=80)
        res_cn = compile_service_release(art_cn)
        self.assertEqual(len(res_cn.ingresses), 1)
        self.assertEqual(res_cn.ingresses[0]["metadata"]["annotations"].get("luma.io/edge-gateway"), "cn-edge")

        # 3. external-edge (requires region=global, domain, port)
        art_ext = _make_svc_art("svc-ext", exposure="external-edge", domain="global.example.com", region="global", port=80)
        res_ext = compile_service_release(art_ext)
        self.assertEqual(len(res_ext.ingresses), 1)
        self.assertEqual(res_ext.ingresses[0]["metadata"]["annotations"].get("luma.io/edge-gateway"), "global-edge")

        # 4. tailscale-relay (requires region=home, domain, port)
        art_ts = _make_svc_art("svc-ts", exposure="tailscale-relay", domain="home.example.com", region="home", platform="linux/arm64", port=80)
        res_ts = compile_service_release(art_ts)
        self.assertEqual(len(res_ts.ingresses), 1)
        self.assertEqual(res_ts.ingresses[0]["metadata"]["annotations"].get("luma.io/relay-transport"), "tailscale-mesh")

        # 5. cloudflare-tunnel (requires domain, port)
        art_cf = _make_svc_art("svc-cf", exposure="cloudflare-tunnel", domain="cf.example.com", region="cn", port=8080)
        res_cf = compile_service_release(art_cf)
        self.assertEqual(len(res_cf.ingresses), 1)
        self.assertEqual(res_cf.ingresses[0]["metadata"]["annotations"].get("luma.io/edge-gateway"), "cloudflare-tunnel")

        # 6. tcp-relay (requires port or publishPort)
        art_tcp = _make_svc_art("svc-tcp", exposure="tcp-relay", port=6379, publish_port=16379, region="cn")
        res_tcp = compile_service_release(art_tcp)
        self.assertEqual(len(res_tcp.ingresses), 1)
        self.assertEqual(res_tcp.ingresses[0]["kind"], "IngressRouteTCP")
        self.assertEqual(res_tcp.services[0]["spec"]["ports"][0]["name"], "tcp")

    def test_invalid_exposure_modes_rejected_in_service_and_compose(self) -> None:
        """Verifies unknown, edge, public, private, internal, and arbitrary strings raise LumaError."""
        invalid_modes = ["unknown", "edge", "public", "private", "internal", "mesh", "custom-edge", "ingress"]

        for bad_mode in invalid_modes:
            # Single service compiler
            art_svc = _make_svc_art(f"bad-{bad_mode}", exposure=bad_mode, domain="example.com", port=8080)
            with self.assertRaises(LumaError, msg=f"Service should reject exposure: {bad_mode}"):
                compile_service_release(art_svc)

            # Compose compiler
            art_comp = _make_comp_art(
                f"bad-comp-{bad_mode}",
                services_compose={"web": {"image": "100.66.177.70:5000/web:v1"}},
                services_luma={"web": {"exposure": bad_mode, "domain": "example.com", "port": 8080}},
            )
            with self.assertRaises(LumaError, msg=f"Compose should reject exposure: {bad_mode}"):
                compile_compose_release(art_comp)

    def test_region_exposure_affinity_enforcement(self) -> None:
        """Verifies cross-region exposure mismatches are strictly rejected."""
        # cn-edge in global or home region
        with self.assertRaises(LumaError):
            compile_service_release(_make_svc_art("cn-in-global", exposure="cn-edge", domain="a.com", region="global"))
        with self.assertRaises(LumaError):
            compile_service_release(_make_svc_art("cn-in-home", exposure="cn-edge", domain="a.com", region="home", platform="linux/arm64"))

        # external-edge in cn or home region
        with self.assertRaises(LumaError):
            compile_service_release(_make_svc_art("ext-in-cn", exposure="external-edge", domain="a.com", region="cn"))
        with self.assertRaises(LumaError):
            compile_service_release(_make_svc_art("ext-in-home", exposure="external-edge", domain="a.com", region="home", platform="linux/arm64"))

        # tailscale-relay in cn or global region
        with self.assertRaises(LumaError):
            compile_service_release(_make_svc_art("ts-in-cn", exposure="tailscale-relay", domain="a.com", region="cn"))
        with self.assertRaises(LumaError):
            compile_service_release(_make_svc_art("ts-in-global", exposure="tailscale-relay", domain="a.com", region="global"))

    def test_missing_required_fields_per_exposure_mode(self) -> None:
        """Verifies missing domain or invalid ports raise LumaError."""
        # cn-edge missing domain
        with self.assertRaises(LumaError) as ctx1:
            compile_service_release(_make_svc_art("cn-no-domain", exposure="cn-edge", domain="", port=80))
        self.assertIn("domain", str(ctx1.exception).lower())

        # external-edge missing domain
        with self.assertRaises(LumaError) as ctx2:
            compile_service_release(_make_svc_art("ext-no-domain", exposure="external-edge", domain="", region="global", port=80))
        self.assertIn("domain", str(ctx2.exception).lower())

        # tcp-relay with invalid/negative port in service release
        with self.assertRaises(LumaError) as ctx3:
            compile_service_release(_make_svc_art("tcp-neg-port", exposure="tcp-relay", port=-1, publish_port=None))
        self.assertIn("valid port", str(ctx3.exception).lower())

        # tcp-relay with port=0 in compose release
        art_comp_zero = _make_comp_art(
            "tcp-comp-zero",
            services_compose={"tcp-svc": {"image": "100.66.177.70:5000/app:v1"}},
            services_luma={"tcp-svc": {"exposure": "tcp-relay", "port": 0}},
        )
        with self.assertRaises(LumaError) as ctx4:
            compile_compose_release(art_comp_zero)
        self.assertIn("valid port", str(ctx4.exception).lower())


class Feature6TCPRelayCollisionsStressTests(unittest.TestCase):
    """Stress tests for TCP relay port collisions across 5+ services."""

    def test_five_services_distinct_tcp_relay_ports(self) -> None:
        """Verifies 5 independent Compose services with unique publishPorts compile cleanly."""
        svcs = {f"svc{i}": {"image": f"100.66.177.70:5000/svc{i}:v1"} for i in range(1, 6)}
        luma_svcs = {
            f"svc{i}": {"exposure": "tcp-relay", "port": 5000, "publishPort": 15000 + i}
            for i in range(1, 6)
        }
        art = _make_comp_art("tcp-5-distinct", services_compose=svcs, services_luma=luma_svcs)
        compiled = compile_compose_release(art)

        self.assertEqual(len(compiled.deployments), 5)
        self.assertEqual(len(compiled.services), 5)
        self.assertEqual(len(compiled.ingresses), 5)

        for i, s in enumerate(compiled.services, start=1):
            self.assertEqual(s["spec"]["ports"][0]["name"], "tcp")
            self.assertEqual(s["spec"]["ports"][0]["port"], 5000)

        for i, ing in enumerate(compiled.ingresses, start=1):
            self.assertEqual(ing.get("kind"), "IngressRouteTCP")
            self.assertEqual(ing["spec"]["entryPoints"], [f"tcp-{15000 + i}"])

    def test_tcp_relay_port_collision_across_five_services(self) -> None:
        """Verifies duplicate publishPort across 5+ services raises collision LumaError."""
        # Collision between svc1 and svc5
        svcs = {f"svc{i}": {"image": f"100.66.177.70:5000/svc{i}:v1"} for i in range(1, 6)}
        luma_svcs = {
            f"svc{i}": {"exposure": "tcp-relay", "port": 5000, "publishPort": 15001 if i == 5 else 15000 + i}
            for i in range(1, 6)
        }
        art1 = _make_comp_art("tcp-col-1-5", services_compose=svcs, services_luma=luma_svcs)
        with self.assertRaises(LumaError) as ctx1:
            compile_compose_release(art1)
        self.assertIn("tcp relay port collision", str(ctx1.exception).lower())
        self.assertIn("15001", str(ctx1.exception))

        # Collision between svc2 and svc4
        luma_svcs["svc5"]["publishPort"] = 15005
        luma_svcs["svc4"]["publishPort"] = 15002
        art2 = _make_comp_art("tcp-col-2-4", services_compose=svcs, services_luma=luma_svcs)
        with self.assertRaises(LumaError) as ctx2:
            compile_compose_release(art2)
        self.assertIn("tcp relay port collision", str(ctx2.exception).lower())
        self.assertIn("15002", str(ctx2.exception))

    def test_tcp_relay_reserved_ports_rejected(self) -> None:
        """Verifies Traefik reserved ports 80 and 443 are rejected for tcp-relay."""
        # Reserved port 80
        art80 = _make_svc_art("tcp-res-80", exposure="tcp-relay", port=80, publish_port=80)
        with self.assertRaises(LumaError) as ctx80:
            compile_service_release(art80)
        self.assertIn("reserved", str(ctx80.exception).lower())
        self.assertIn("80", str(ctx80.exception))

        # Reserved port 443
        art443 = _make_svc_art("tcp-res-443", exposure="tcp-relay", port=443, publish_port=443)
        with self.assertRaises(LumaError) as ctx443:
            compile_service_release(art443)
        self.assertIn("reserved", str(ctx443.exception).lower())
        self.assertIn("443", str(ctx443.exception))

        # Reserved port 80 in 5-service compose stack
        svcs = {f"svc{i}": {"image": f"100.66.177.70:5000/svc{i}:v1"} for i in range(1, 6)}
        luma_svcs = {
            f"svc{i}": {"exposure": "tcp-relay", "port": 5000, "publishPort": 80 if i == 3 else 15000 + i}
            for i in range(1, 6)
        }
        art_comp_80 = _make_comp_art("tcp-comp-res-80", services_compose=svcs, services_luma=luma_svcs)
        with self.assertRaises(LumaError):
            compile_compose_release(art_comp_80)

    def test_tcp_relay_port_collision_between_publishport_and_fallback_port(self) -> None:
        """Verifies collision when one service specifies publishPort and another uses unmapped port."""
        svcs = {f"svc{i}": {"image": f"100.66.177.70:5000/svc{i}:v1"} for i in range(1, 6)}
        luma_svcs = {
            f"svc{i}": {"exposure": "tcp-relay", "port": 5000, "publishPort": 15000 + i}
            for i in range(1, 6)
        }
        # Svc5 omits publishPort but declares port=15001 matching svc1's publishPort
        del luma_svcs["svc5"]["publishPort"]
        luma_svcs["svc5"]["port"] = 15001

        art = _make_comp_art("tcp-col-port-fallback", services_compose=svcs, services_luma=luma_svcs)
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("tcp relay port collision", str(ctx.exception).lower())
        self.assertIn("15001", str(ctx.exception))


class Feature6ExposureNoneSuppressionStressTests(unittest.TestCase):
    """Stress tests for exposure: none with/without domain, ensuring 0 Ingress resources."""

    def test_service_exposure_none_with_and_without_domain(self) -> None:
        """Verifies exposure: none suppresses all Ingress manifests regardless of domain."""
        # 1. No domain
        art_nodomain = _make_svc_art("none-nodomain", exposure="none", port=8080)
        res_nodomain = compile_service_release(art_nodomain)
        self.assertEqual(len(res_nodomain.ingresses), 0)
        self.assertEqual(len(res_nodomain.services), 1)
        self.assertEqual(
            res_nodomain.helm_release["spec"]["values"]["gatewayProbeUrl"],
            "http://none-nodomain.luma-prod.svc.cluster.local:8080",
        )

        # 2. With domain (must NOT fall back to cn-edge or external-edge)
        art_withdomain = _make_svc_art(
            "none-withdomain",
            exposure="none",
            domain="internal-only.corp.net",
            port=8080,
        )
        res_withdomain = compile_service_release(art_withdomain)
        self.assertEqual(len(res_withdomain.ingresses), 0)
        self.assertEqual(len(res_withdomain.services), 1)
        self.assertEqual(
            res_withdomain.helm_release["spec"]["values"]["gatewayProbeUrl"],
            "http://none-withdomain.luma-prod.svc.cluster.local:8080",
        )

    def test_compose_exposure_none_multi_service_suppression(self) -> None:
        """Verifies compose services with exposure: none emit zero Ingress resources."""
        art = _make_comp_art(
            "compose-none-stack",
            services_compose={
                "web": {"image": "100.66.177.70:5000/web:v1"},
                "api": {"image": "100.66.177.70:5000/api:v1"},
                "worker": {"image": "100.66.177.70:5000/worker:v1"},
            },
            services_luma={
                "web": {"exposure": "none", "domain": "web.internal", "port": 3000},
                "api": {"exposure": "none", "port": 4000},
                "worker": {"exposure": "none", "port": 0},
            },
        )
        compiled = compile_compose_release(art)
        self.assertEqual(len(compiled.deployments), 3)
        self.assertEqual(len(compiled.ingresses), 0)
        # web and api have ports > 0 -> 2 services; worker has port=0 -> no service
        self.assertEqual(len(compiled.services), 2)

    def test_compose_mixed_exposure_modes_isolation(self) -> None:
        """Verifies that in a mixed stack, only services declaring external exposure emit Ingress."""
        art = _make_comp_art(
            "mixed-exposure-stack",
            services_compose={
                "frontend": {"image": "100.66.177.70:5000/frontend:v1"},
                "backend-api": {"image": "100.66.177.70:5000/backend:v1"},
                "cache-tcp": {"image": "100.66.177.70:5000/cache:v1"},
                "background-worker": {"image": "100.66.177.70:5000/worker:v1"},
            },
            services_luma={
                "frontend": {"exposure": "cn-edge", "domain": "app.example.com", "port": 80},
                "backend-api": {"exposure": "none", "domain": "api.internal", "port": 5000},
                "cache-tcp": {"exposure": "tcp-relay", "port": 6379, "publishPort": 16379},
                "background-worker": {"exposure": "none", "port": 0},
            },
        )
        compiled = compile_compose_release(art)
        self.assertEqual(len(compiled.deployments), 4)

        # Ingresses: exactly 2 (frontend -> Ingress, cache-tcp -> IngressRouteTCP)
        self.assertEqual(len(compiled.ingresses), 2)
        kinds = {ing.get("kind") for ing in compiled.ingresses}
        self.assertEqual(kinds, {"Ingress", "IngressRouteTCP"})

        ing_names = {ing["metadata"]["name"] for ing in compiled.ingresses}
        self.assertEqual(ing_names, {"mixed-exposure-stack-frontend", "mixed-exposure-stack-cache-tcp"})
