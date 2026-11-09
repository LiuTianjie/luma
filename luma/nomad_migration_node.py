"""Typed, resumable host steps for the ordinary Luma upgrade workflow.

No arbitrary shell/config is accepted from a task. The installed Luma renderer
owns the configuration; version downloads are restricted to pinned releases.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Mapping

from .errors import LumaError
from .local import LocalExecutor
from .nomad_api import NomadApi
from .nomad_firewall import GUARD, install_guard, network_policy
from .nomad_node import _nomad_arch, detect_os, install_nomad_commands, render_agent_config
from .nomad_pki import create_csr, expires_soon, install_certificate, tls_paths
from .nomad_security import ADMIN_ROOT, NOMAD_RELEASES, NOMAD_VERSION, deny_host_root, mesh_ip, nomad_token, private_read, private_write, read_private_json, security_root

CAPABILITY = "nomad-security-upgrade-v1"
CONFIG = Path("/etc/nomad.d/nomad.hcl")
BINARY = Path("/usr/local/bin/nomad")


def api() -> NomadApi:
    return NomadApi("http://127.0.0.1:4646", token=nomad_token(role="agent"), credential_role="agent")


def version_path(current: str) -> list[str]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", current):
        raise LumaError("cannot determine the installed Nomad release")
    actual = tuple(map(int, current.split(".")))
    if actual < (1, 9, 0) or actual > tuple(map(int, NOMAD_VERSION.split("."))):
        raise LumaError(f"Nomad {current} is outside the supported automatic upgrade path; no downgrade was attempted")
    return [v for v in NOMAD_RELEASES if tuple(map(int, v.split("."))) > actual]


def snapshot(*, with_csr: bool = True) -> dict[str, Any]:
    if CONFIG.is_symlink():
        raise LumaError("Nomad config must not be a symlink")
    fragments = [p.name for p in CONFIG.parent.iterdir() if p.suffix in {".hcl", ".json"} and p.name != CONFIG.name]
    if fragments:
        raise LumaError("automatic security upgrade requires one Nomad config; additional fragments: " + ", ".join(sorted(fragments)))
    raw = CONFIG.read_text()
    own = api().request("GET", "/v1/agent/self", timeout=10)
    cfg = own.get("config") or {}
    if cfg.get("DataDir") != "/opt/nomad/data" or cfg.get("Region", "global") != "global" or cfg.get("Datacenter", "dc1") != "dc1":
        raise LumaError("custom Nomad data directory, region or datacenter needs an explicit migration; existing config was retained")
    node_id = str((own.get("stats", {}).get("client") or {}).get("node_id") or "")
    if not node_id:
        raise LumaError("Nomad client identity is unavailable; refusing an uncorrelated host upgrade")
    node = api().request("GET", "/v1/node/" + node_id, timeout=10)
    meta = node.get("Meta") or {}
    address = mesh_ip(str(meta.get("luma_tailscale_ip") or (cfg.get("AdvertiseAddrs") or {}).get("HTTP") or (cfg.get("AdvertiseAddrs") or {}).get("Http") or node.get("Address") or "").removesuffix(":4646"))
    cpu = re.search(r"\bcpu_total_compute\s*=\s*([0-9]+)", raw)
    version = str((cfg.get("Version") or {}).get("Version") or "")
    version_path(version)
    return {"schema": 1, "nodeId": node_id, "node": str(meta.get("luma_node_name") or node.get("Name") or ""),
            "nomadName": str(node.get("Name") or ""), "address": address, "os": detect_os(),
            "role": "server" if (cfg.get("Server") or {}).get("Enabled") else "client",
            "region": str(meta.get("region") or ""), "extraMeta": {str(k): str(v) for k, v in meta.items() if k not in {"region", "luma_node_name", "luma_tailscale_ip", "luma_host_access"}},
            "cpu": int(cpu.group(1)) if cpu else None, "version": version,
            "baselineSHA256": hashlib.sha256(raw.encode()).hexdigest(),
            "acl": (cfg.get("ACL") or {}).get("Enabled") is True,
            "tlsHTTP": (cfg.get("TLSConfig") or {}).get("EnableHTTP") is True,
            "tlsRPC": (cfg.get("TLSConfig") or {}).get("EnableRPC") is True,
            "rpcUpgradeMode": (cfg.get("TLSConfig") or {}).get("RPCUpgradeMode") is True,
            "certificateRenewalRequired": expires_soon(ADMIN_ROOT / "tls"),
            "nodeStatus": node.get("Status"), **({"csr": create_csr()} if with_csr else {})}


def validate_mounts(mounts: list[dict[str, Any]], *, system: bool = False, allow_new: bool = False) -> None:
    remote = LocalExecutor()
    for mount in mounts:
        if mount.get("type") == "bind":
            source = str(mount.get("source") or "")
            if detect_os() == "darwin":
                raise LumaError("cannot prove a bind source inside the macOS Docker VM; use an engine-local named volume or an explicitly verified storage host")
            resolved = os.path.realpath(source)
            if not system:
                deny_host_root({"TaskGroups": [{"Tasks": [{"Config": {"mount": [{**mount, "source": resolved}]}}]}]})
        elif mount.get("type") == "volume":
            name = str(mount.get("source") or "")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                raise LumaError("invalid Docker data volume name")
            # Match Nomad's local socket, independent of DOCKER_HOST/context.
            result = remote.run_result("docker --host unix:///var/run/docker.sock volume inspect " + shlex.quote(name), timeout=20)
            if result.code:
                if allow_new and "no such volume" in result.output.lower():
                    continue
                raise LumaError(f"cannot verify existing data volume {name}; no storage was created or moved")
            try:
                volume = json.loads(result.output)[0]
            except (ValueError, IndexError, TypeError):
                raise LumaError("Docker volume inspection returned an invalid response") from None
            options = volume.get("Options") or {}
            if volume.get("Driver") != "local":
                raise LumaError(f"data volume {name} uses an unreviewed Docker driver")
            device = str(options.get("device") or "")
            if options and detect_os() == "darwin":
                raise LumaError("cannot prove Docker volume driver paths inside the macOS VM; only local volumes without driver options are automatically accepted")
            if options and not device:
                raise LumaError("Docker volume driver options have no verifiable storage device")
            if device.startswith("/") and not system:
                deny_host_root({"TaskGroups": [{"Tasks": [{"Config": {"mount": [{"type": "bind", "source": os.path.realpath(device), "readonly": bool(mount.get("readonly"))}]}}]}]})
            elif options and str(options.get("type") or "") not in {"nfs", "nfs4", "cifs"}:
                raise LumaError("Docker volume device is not a verifiable local or network data source")


def _restart(remote: LocalExecutor, *, os_name: str) -> None:
    unit = install_nomad_commands(os_name=os_name, arch=os.uname().machine, config_hcl="")
    if os_name == "linux":
        private_write(Path("/etc/systemd/system/nomad.service"), unit["service_unit"], mode=0o644)
        remote.run("systemctl daemon-reload && systemctl enable nomad && systemctl restart nomad", timeout=60)
    else:
        path = Path("/Library/LaunchDaemons/io.luma.nomad.plist")
        private_write(path, unit["service_unit"], mode=0o644)
        remote.run("launchctl bootout system/io.luma.nomad >/dev/null 2>&1 || true; launchctl bootstrap system /Library/LaunchDaemons/io.luma.nomad.plist", timeout=60)


def _wait(version: str, node_id: str, *, timeout: float = 90) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = snapshot(with_csr=False)
            if result["version"] == version and result["nodeId"] == node_id and result["nodeStatus"] == "ready":
                return result
        except LumaError:
            pass
        if time.monotonic() >= deadline:
            raise LumaError("Nomad did not return healthy with its original node identity; the upgrade remains incomplete and no automatic downgrade was attempted")
        time.sleep(1)


def _network(payload: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
    from .bootstrap import _mac_mesh_interface
    manager = mesh_ip(str(payload.get("managerAddress") or facts["address"]))
    peers = [mesh_ip(str(p)) for p in payload.get("peers") or []]
    server = facts["role"] == "server"
    return network_policy(os_name=facts["os"], address=facts["address"], rpc_peers=peers if server else [manager],
                          http_peers=[] if server else [manager], serf_peers=[],
                          interface=_mac_mesh_interface(LocalExecutor(), facts["address"], peer=manager) if facts["os"] == "darwin" else "")


def _download(version: str, destination: Path, *, proxy: str = "") -> Path:
    platform = detect_os() + "_" + _nomad_arch(os.uname().machine)
    digest = NOMAD_RELEASES.get(version, {}).get(platform)
    if not digest:
        raise LumaError("Nomad upgrade has no pinned binary for this release/platform")
    if proxy:
        from urllib.parse import urlsplit
        parsed = urlsplit(proxy)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise LumaError("invalid Nomad download proxy")
    archive = destination / "nomad.zip"
    for attempt in ([proxy, ""] if proxy else [""]):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": attempt} if attempt else {}))
        try:
            with opener.open(f"https://releases.hashicorp.com/nomad/{version}/nomad_{version}_{platform}.zip", timeout=60) as response, archive.open("wb") as out:
                shutil.copyfileobj(response, out)
            break
        except OSError:
            if not attempt:
                raise LumaError("Nomad download failed; the installed binary was retained") from None
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise LumaError("Nomad binary checksum mismatch; the running binary was retained")
    with zipfile.ZipFile(archive) as bundle:
        binary = destination / "nomad"
        with bundle.open("nomad") as source, binary.open("wb") as out:
            shutil.copyfileobj(source, out)
    binary.chmod(0o755)
    return binary


def _request_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _save_operation(record: dict[str, Any]) -> None:
    private_write(ADMIN_ROOT / "operation.json", json.dumps(record))


def _apply_operation(record: dict[str, Any]) -> dict[str, Any]:
    """Finish an already validated local intent without requiring the old API.

    The intent is committed before changing the binary, config or transport.
    It is owned by root outside Control's mount, and can therefore bridge the
    period when neither the old HTTP API nor the new ACL reader is available.
    """
    facts, payload = record["facts"], record["request"]
    if record.get("requestSHA256") != _request_digest(payload):
        raise LumaError("saved Nomad operation is inconsistent; refusing recovery")
    if CONFIG.is_symlink() or BINARY.is_symlink():
        raise LumaError("Nomad binary/config must not be symlinks during recovery")
    current_hash = hashlib.sha256(CONFIG.read_bytes()).hexdigest()
    if current_hash not in {facts["baselineSHA256"], record["configSHA256"]}:
        raise LumaError("Nomad configuration changed after the saved operation; refusing recovery")
    if record.get("phase") == "completed":
        return record["result"]
    remote = LocalExecutor()
    install_guard(remote, _network(payload, facts))
    if payload["op"] == "version":
        current_binary = hashlib.sha256(BINARY.read_bytes()).hexdigest()
        if current_binary not in {record["previousBinarySHA256"], record["binarySHA256"]}:
            raise LumaError("Nomad binary changed after the saved operation; refusing recovery")
        if current_binary != record["binarySHA256"]:
            staged = ADMIN_ROOT / "upgrades" / record["version"] / "nomad"
            if staged.is_symlink() or hashlib.sha256(staged.read_bytes()).hexdigest() != record["binarySHA256"]:
                raise LumaError("saved Nomad binary failed integrity verification")
            fd, pending = tempfile.mkstemp(prefix=".nomad-upgrade-", dir=BINARY.parent)
            try:
                with os.fdopen(fd, "wb") as out, staged.open("rb") as source:
                    shutil.copyfileobj(source, out)
                    os.fchmod(out.fileno(), 0o755)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(pending, BINARY)
            finally:
                Path(pending).unlink(missing_ok=True)
    else:
        config = record["config"]
        if hashlib.sha256(config.encode()).hexdigest() != record["configSHA256"]:
            raise LumaError("saved Nomad configuration failed integrity verification")
        if current_hash != record["configSHA256"]:
            private_write(CONFIG, config)
        # Never return to HTTP after this intent has selected HTTPS, including
        # when the process dies immediately before the first service restart.
        private_write(security_root() / "transport.json", json.dumps({"http": bool(payload.get("tlsHTTP")), "address": facts["address"]}))
        private_write(security_root() / "node.json", json.dumps(record["profile"]))
        if facts["role"] == "client":
            private_write(security_root() / "cluster.json", json.dumps({"schema": 1, "aclRequired": bool(payload.get("acl")), "tlsRequired": bool(payload.get("tlsHTTP")), "nomadVersion": record["version"]}))
    if record.get("phase") != "restarted":
        _restart(remote, os_name=facts["os"])
        record["phase"] = "restarted"
        _save_operation(record)
    if facts["role"] == "server" and payload.get("acl") and not facts["acl"]:
        result = {**facts, "baselineSHA256": record["configSHA256"], "aclBootstrapPending": True}
    else:
        result = _wait(record["version"], facts["nodeId"])
    record.update(phase="completed", result=result)
    _save_operation(record)
    return result


def resume_operation(*, allow_server: bool = False, payload: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    record = read_private_json(ADMIN_ROOT / "operation.json", optional=True)
    if not record:
        return None
    repeated = payload is not None and record.get("requestSHA256") == _request_digest(payload)
    if record.get("phase") == "completed" and not repeated:
        return None
    facts = record.get("facts") or {}
    if facts.get("role") == "server" and not allow_server:
        raise LumaError("only the independent host supervisor can resume a Nomad server operation")
    if payload and payload.get("op") != "inspect" and (payload.get("nodeId") != facts.get("nodeId") or payload.get("address") != facts.get("address")):
        raise LumaError("pending Nomad operation belongs to another node identity")
    result = _apply_operation(record)
    return result if repeated or payload is None else None


def execute(payload: Mapping[str, Any], *, allow_server: bool = False) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise LumaError("Nomad security migration requires the root node agent")
    if payload.get("schema") != 1 or payload.get("op") not in {"inspect", "preflight", "version", "configure", "verify", "certificate-request", "renew", "storage"}:
        raise LumaError("unsupported Nomad security upgrade step")
    operation = str(payload["op"])
    if operation in {"certificate-request", "renew"}:
        # Renewal must work after a long-offline node's Nomad certificate has
        # expired. Trust the protected, previously verified local identity and
        # authenticated Luma task channel; never fall back to plain HTTP.
        profile = read_private_json(security_root() / "node.json")
        if not profile.get("nodeId") or not profile.get("address"):
            raise LumaError("certificate renewal requires a previously verified local node identity")
        if operation == "certificate-request":
            return {"nodeId": profile["nodeId"], "address": profile["address"], "csr": create_csr(), "certificateRenewalRequired": expires_soon(ADMIN_ROOT / "tls")}
        if profile["nodeId"] != payload.get("nodeId") or profile["address"] != payload.get("address"):
            raise LumaError("certificate renewal does not match the protected local identity")
        install_certificate(payload.get("certificate") or {})
        command = "systemctl reload nomad" if detect_os() == "linux" else "launchctl kill SIGHUP system/io.luma.nomad"
        LocalExecutor().run(command, timeout=30)
        _wait(profile["nomadVersion"], profile["nodeId"])
        return {"renewed": True, "nodeId": profile["nodeId"]}
    recovered = resume_operation(allow_server=allow_server, payload=payload)
    if recovered is not None:
        return recovered
    facts = snapshot()
    if operation == "inspect":
        return facts
    if payload.get("nodeId") != facts["nodeId"] or payload.get("address") != facts["address"]:
        raise LumaError("Nomad security task does not match this host's registered identity")
    if facts["role"] == "server" and not allow_server and operation != "storage":
        raise LumaError("the Nomad server is upgraded only by its independent host supervisor")
    if operation == "storage":
        validate_mounts(list(payload.get("mounts") or []), allow_new=True)
        profile = read_private_json(security_root() / "node.json")
        if not facts["acl"] or not facts["tlsHTTP"] or not facts["tlsRPC"]:
            raise LumaError("finish the automatic Nomad security upgrade before provisioning application storage")
        if not profile.get("trustedHost"):
            if facts["role"] == "server":
                raise LumaError("manager storage permissions require its host-owned security profile")
            network = read_private_json(security_root() / "network.json")
            execute({"schema": 1, "op": "configure", "nodeId": facts["nodeId"], "address": facts["address"],
                     "managerAddress": network["rpcPeers"][0], "version": NOMAD_VERSION,
                     "baselineSHA256": facts["baselineSHA256"], "acl": True, "tlsHTTP": True, "trustedHost": True,
                     "certificate": {"ca": private_read(ADMIN_ROOT / "tls" / "ca.pem"), "cert": private_read(ADMIN_ROOT / "tls" / "cert.pem")}})
        return {"verified": True, "nodeId": facts["nodeId"], "trustedHost": True}
    if operation == "preflight":
        validate_mounts(list(payload.get("mounts") or []))
        validate_mounts(list(payload.get("systemMounts") or []), system=True)
        return {**facts, "mountsVerified": True}
    if operation == "verify":
        LocalExecutor().run(shlex.quote(GUARD), timeout=20)
        own = api().request("GET", "/v1/agent/self")
        cfg = own.get("config") or {}
        options = (cfg.get("Client") or {}).get("Options") or {}
        checks = {"version": facts["version"] == NOMAD_VERSION, "acl": facts["acl"], "tlsHTTP": facts["tlsHTTP"],
                  "tlsRPC": facts["tlsRPC"], "rpcStrict": not facts["rpcUpgradeMode"], "ready": facts["nodeStatus"] == "ready",
                  "dockerOnly": options.get("driver.allowlist") == "docker", "networkGuard": True}
        try:
            NomadApi("http://127.0.0.1:4646", credential_role="agent").request("GET", "/v1/jobs", timeout=10)
            checks["anonymousDenied"] = False
        except LumaError as exc:
            checks["anonymousDenied"] = "Nomad API error 403:" in str(exc)
        profile = read_private_json(security_root() / "node.json")
        checks["configUnchanged"] = profile.get("configSHA256") == facts["baselineSHA256"]
        plugin = next((p.get("Config") or {} for p in cfg.get("Plugins") or [] if p.get("Name") == "docker"), {})
        volumes = plugin.get("volumes") or {}
        if isinstance(volumes, list):
            volumes = volumes[0] if len(volumes) == 1 else {}
        checks["unprivilegedDriver"] = plugin.get("allow_privileged") is False
        checks["volumeProfile"] = volumes.get("enabled") is bool(profile.get("trustedHost"))
        caps = {str(cap).lower() for cap in plugin.get("allow_caps") or []}
        checks["restrictedCapabilities"] = bool(caps) and not caps & {"all", "sys_admin", "sys_ptrace", "net_admin", "dac_read_search"}
        tls = cfg.get("TLSConfig") or {}
        checks["mutualTLS"] = tls.get("VerifyHTTPSClient") is True and tls.get("VerifyServerHostname") is True
        addresses = cfg.get("Addresses") or {}
        checks["privateListeners"] = set(str(addresses.get("HTTP") or "").split()) == {"127.0.0.1", facts["address"]} and addresses.get("RPC") == facts["address"] and addresses.get("Serf") == facts["address"]
        if facts["role"] == "server":
            checks["strictEnrollment"] = ((cfg.get("Server") or {}).get("ClientIntroduction") or {}).get("Enforcement") == "strict"
        if not all(checks.values()):
            raise LumaError("Nomad security verification failed: " + ", ".join(k for k, ok in checks.items() if not ok))
        profile.update(nodeId=facts["nodeId"], address=facts["address"])
        private_write(security_root() / "node.json", json.dumps(profile))
        return {"nodeId": facts["nodeId"], "version": facts["version"], "checks": checks, "verifiedAt": int(time.time())}

    remote = LocalExecutor()
    install_guard(remote, _network(payload, facts))
    if operation == "version":
        target = str(payload.get("version") or "")
        if target == facts["version"]:
            return facts
        route = version_path(facts["version"])
        if not route or route[0] != target:
            raise LumaError("Nomad upgrade must traverse each supported minor release; downgrades are disabled")
        with tempfile.TemporaryDirectory(prefix="luma-nomad-upgrade-") as directory:
            binary = _download(target, Path(directory), proxy=str(payload.get("proxy") or ""))
            remote.run(shlex.join([str(binary), "config", "validate", str(CONFIG.parent)]), timeout=30)
            backup = ADMIN_ROOT / "backups" / ("before-" + target)
            backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not (backup / "nomad.hcl").exists():
                private_write(backup / "nomad.hcl", CONFIG.read_text())
                shutil.copy2(BINARY, backup / "nomad")
            staged = ADMIN_ROOT / "upgrades" / target / "nomad"
            staged.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with binary.open("rb") as source, staged.open("wb") as out:
                shutil.copyfileobj(source, out)
                os.fchmod(out.fileno(), 0o700)
                out.flush()
                os.fsync(out.fileno())
            record = {"schema": 1, "phase": "prepared", "facts": facts, "request": dict(payload),
                      "requestSHA256": _request_digest(payload), "version": target,
                      "configSHA256": facts["baselineSHA256"],
                      "previousBinarySHA256": hashlib.sha256(BINARY.read_bytes()).hexdigest(),
                      "binarySHA256": hashlib.sha256(staged.read_bytes()).hexdigest()}
            _save_operation(record)
        return _apply_operation(record)

    version = str(payload.get("version") or NOMAD_VERSION)
    if version != facts["version"]:
        raise LumaError("finish the server-first binary upgrade before applying security configuration")
    acl, tls_http = bool(payload.get("acl")), bool(payload.get("tlsHTTP"))
    if facts["acl"] and not acl or facts["tlsHTTP"] and not tls_http:
        raise LumaError("automatic upgrade cannot disable ACLs or HTTPS")
    install_certificate(payload.get("certificate") or {})
    config = render_agent_config(os_name=facts["os"], role=facts["role"], tailscale_ip=facts["address"],
                                 region=facts["region"], node_name=facts["node"],
                                 server_addrs=None if facts["role"] == "server" else [mesh_ip(str(payload["managerAddress"])) + ":4647"],
                                 cpu_total_compute=facts["cpu"], extra_meta=facts["extraMeta"],
                                 acl_enabled=acl, trusted_host=bool(payload.get("trustedHost")), version=version,
                                 tls=tls_paths(ADMIN_ROOT / "tls"), tls_http=tls_http,
                                 rpc_upgrade_mode=bool(payload.get("rpcUpgradeMode")), introduction_strict=acl)
    config_hash = hashlib.sha256(config.encode()).hexdigest()
    expected = str(payload.get("baselineSHA256") or "")
    if facts["baselineSHA256"] not in {expected, config_hash}:
        raise LumaError("Nomad configuration changed after the automatic upgrade preflight; refusing to overwrite it")
    if acl:
        private_read(ADMIN_ROOT / "agent.token")
    with tempfile.TemporaryDirectory(prefix="luma-nomad-config-") as directory:
        stage = Path(directory) / "nomad.hcl"
        private_write(stage, config)
        remote.run(shlex.join([str(BINARY), "config", "validate", str(stage)]), timeout=30)
    if facts["baselineSHA256"] != config_hash:
        private_write(ADMIN_ROOT / "backups" / ("config-" + facts["baselineSHA256"]) / "nomad.hcl", CONFIG.read_text())
    profile = {"schema": 1, "node": facts["node"], "role": facts["role"], "nomadVersion": version,
               "nodeId": facts["nodeId"], "address": facts["address"],
               "aclRequired": acl, "trustedHost": bool(payload.get("trustedHost")), "tls": tls_paths(ADMIN_ROOT / "tls"), "configSHA256": config_hash}
    record = {"schema": 1, "phase": "prepared", "facts": facts, "request": dict(payload),
              "requestSHA256": _request_digest(payload), "version": version,
              "config": config, "configSHA256": config_hash, "profile": profile}
    _save_operation(record)
    return _apply_operation(record)
