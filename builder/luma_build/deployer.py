"""Standard public-API client from luma-build to Luma Control.

``luma-build`` treats ``luma-control`` strictly as an external deployment target:
it sends only the built immutable ``image@sha256:...`` and deployment YAML to
``POST /v1/deployments/stream`` or ``POST /v1/compose-deployments/stream``.
No internal Control Python modules or database tables are shared.
"""
from __future__ import annotations

import hashlib
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

import yaml

from .errors import BuildError
from .scheduler import normalize_platform, parse_platforms


def _normalize_app_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(value or "").strip().lower()).strip("-")


def _extract_manifest_name(manifest_text: str) -> str:
    if not manifest_text or not manifest_text.strip():
        return ""
    try:
        data = yaml.safe_load(manifest_text)
    except yaml.YAMLError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("name") or "").strip()


def _authorized_app_names(build_record: dict[str, Any], request: dict[str, Any]) -> list[str]:
    expected: list[str] = []
    for raw in (
        build_record.get("appName"),
        build_record.get("app_name"),
        request.get("appName"),
        request.get("app_name"),
        _extract_manifest_name(str(request.get("manifest") or "")),
    ):
        clean = str(raw or "").strip()
        if clean and _normalize_app_slug(clean) not in {_normalize_app_slug(item) for item in expected}:
            expected.append(clean)
    return expected


def _validate_manifest_app_name(manifest_data: dict[str, Any], authorized_names: list[str]) -> None:
    if not authorized_names:
        return
    actual = str(manifest_data.get("name") or "").strip()
    actual_slug = _normalize_app_slug(actual)
    for expected in authorized_names:
        expected_slug = _normalize_app_slug(expected)
        if not expected_slug:
            continue
        if not actual_slug:
            raise BuildError(
                f"deployment manifest is missing required name matching authorized app {expected!r}"
            )
        if actual_slug != expected_slug:
            raise BuildError(
                f"deployment manifest name {actual!r} does not match authorized app {expected!r}"
            )


def extract_manifest_target_hints(manifest_text: str) -> dict[str, list[str]]:
    """Extract explicit build platforms and target node/region placement from a manifest."""
    empty: dict[str, list[str]] = {"platforms": [], "nodes": [], "regions": []}
    if not manifest_text or not manifest_text.strip():
        return empty
    try:
        data = yaml.safe_load(manifest_text)
    except yaml.YAMLError:
        return empty
    if not isinstance(data, dict):
        return empty

    platforms: list[str] = []
    nodes: list[str] = []
    regions: list[str] = []

    def _add_platforms(raw: Any) -> None:
        if not raw:
            return
        for plat in parse_platforms(raw):
            if plat not in platforms:
                platforms.append(plat)

    build_block = data.get("build") if isinstance(data.get("build"), dict) else {}
    _add_platforms(build_block.get("platforms") or build_block.get("platform"))

    top_node = str(data.get("node") or "").strip()
    top_region = str(data.get("region") or "").strip()
    services = data.get("services")
    if isinstance(services, dict) and services:
        for svc in services.values():
            if not isinstance(svc, dict):
                continue
            _add_platforms(svc.get("platform"))
            svc_build = svc.get("build") if isinstance(svc.get("build"), dict) else {}
            _add_platforms(svc_build.get("platforms") or svc_build.get("platform"))
            eff_node = str(svc.get("node") or top_node).strip()
            eff_region = str(svc.get("region") or top_region).strip()
            if eff_node:
                if eff_node not in nodes:
                    nodes.append(eff_node)
            elif eff_region and eff_region not in regions:
                regions.append(eff_region)
    else:
        if top_node:
            nodes.append(top_node)
        elif top_region:
            regions.append(top_region)

    return {"platforms": platforms, "nodes": nodes, "regions": regions}


def extract_manifest_platforms(manifest_text: str) -> list[str]:
    """Extract explicit ``build.platforms`` / ``build.platform`` from an attached manifest."""
    return extract_manifest_target_hints(manifest_text)["platforms"]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(req.full_url, code, f"unexpected redirect ({code})", headers, fp)


def _standard_opener(*, insecure: bool) -> urllib.request.OpenerDirector:
    handlers: list[Any] = [_NoRedirectHandler()]
    if insecure:
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    return urllib.request.build_opener(*handlers)


class ControlAuthVerifier:
    """Verify operator Luma tokens via Control's read-only ``/v1/auth/login/verify``.

    Caches only SHA-256 digests of verified tokens for ``ttl_seconds`` so CLI
    calls remain sub-millisecond while token rotation on Control takes effect
    within one TTL window. Plaintext tokens are never cached.
    """

    def __init__(self, endpoint: str, *, insecure: bool = False, ttl_seconds: float = 60.0) -> None:
        clean = str(endpoint or "").strip().rstrip("/")
        if clean and not clean.startswith(("https://", "http://")):
            raise BuildError(f"invalid Luma Control URL: {endpoint}")
        self.endpoint = clean
        self.insecure = insecure
        self.ttl_seconds = max(float(ttl_seconds), 0.0)
        self._lock = threading.Lock()
        self._verified_until: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint)

    def _opener(self) -> urllib.request.OpenerDirector:
        return _standard_opener(insecure=self.insecure)

    def verify(self, token: str) -> bool:
        clean = str(token or "").strip()
        if not self.enabled or not clean or clean.startswith("lbr_"):
            return False
        digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()
        now = time.monotonic()
        with self._lock:
            exp = self._verified_until.get(digest, 0.0)
            if exp > now:
                return True

        req = urllib.request.Request(
            f"{self.endpoint}/v1/auth/login/verify",
            data=b"{}",
            method="POST",
            headers={
                "Authorization": f"Bearer {clean}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener().open(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                ok = int(resp.status) == 200 and isinstance(payload, dict) and bool(payload.get("clusterId"))
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                with self._lock:
                    self._verified_until.pop(digest, None)
                return False
            raise BuildError(
                f"Luma Control token verification failed with HTTP {exc.code}",
                status_code=503,
            ) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise BuildError(
                f"unable to reach Luma Control to verify login token: {exc}",
                status_code=503,
            ) from exc

        if ok and self.ttl_seconds > 0:
            with self._lock:
                if len(self._verified_until) >= 64:
                    expired = [k for k, v in self._verified_until.items() if v <= now]
                    for k in expired:
                        self._verified_until.pop(k, None)
                    if len(self._verified_until) >= 64:
                        oldest = min(self._verified_until, key=self._verified_until.get)  # type: ignore[arg-type]
                        self._verified_until.pop(oldest, None)
                self._verified_until[digest] = time.monotonic() + self.ttl_seconds
        return ok


class ControlDeployClient:
    def __init__(self, endpoint: str, deploy_token: str, *, insecure: bool = False) -> None:
        clean = str(endpoint or "").strip().rstrip("/")
        if clean and not clean.startswith(("https://", "http://")):
            raise BuildError(f"invalid Luma Control URL: {endpoint}")
        self.endpoint = clean
        self.deploy_token = str(deploy_token or "").strip()
        self.insecure = insecure
        self._placement_lock = threading.Lock()
        self._placement_cache: dict[str, Any] = {}
        self._placement_cached_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.deploy_token)

    def _opener(self) -> urllib.request.OpenerDirector:
        return _standard_opener(insecure=self.insecure)

    def cluster_placement_platforms(self, *, ttl_seconds: float = 15.0) -> dict[str, Any]:
        """Query Luma Control ``GET /v1/status`` for target node and region platforms."""
        empty: dict[str, Any] = {"nodes": {}, "regions": {}}
        if not self.configured:
            return empty
        now = time.monotonic()
        with self._placement_lock:
            if self._placement_cache and (now - self._placement_cached_at) < max(ttl_seconds, 0.0):
                return self._placement_cache
        try:
            payload = self.request_json("GET", "/v1/status", timeout=4)
        except Exception:
            with self._placement_lock:
                self._placement_cache = empty
                self._placement_cached_at = time.monotonic() - max(ttl_seconds - 5.0, 0.0)
            return empty

        nodes_block = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
        items = nodes_block.get("items") if isinstance(nodes_block.get("items"), list) else []
        by_node: dict[str, str] = {}
        ready_by_region: dict[str, set[str]] = {}
        all_by_region: dict[str, set[str]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_plat = str(item.get("nativePlatform") or "").strip()
            if not raw_plat:
                os_name = str(item.get("agentOs") or "").strip().lower()
                arch = str(item.get("agentArch") or "").strip().lower()
                if os_name == "darwin":
                    os_name = "linux"
                if os_name and arch:
                    raw_plat = f"{os_name}/{arch}"
            if not raw_plat:
                continue
            try:
                plat = normalize_platform(raw_plat)
            except BuildError:
                continue
            if not plat:
                continue

            aliases = [
                item.get("name"),
                item.get("displayName"),
                item.get("hostname"),
                *(item.get("aliases") if isinstance(item.get("aliases"), list) else []),
            ]
            for alias in aliases:
                clean_alias = str(alias or "").strip().lower()
                if clean_alias:
                    by_node[clean_alias] = plat

            region = str(item.get("region") or "").strip().lower()
            if region:
                all_by_region.setdefault(region, set()).add(plat)
                agent_status = str(item.get("agentStatus") or "").strip().lower()
                if agent_status in {"online", "ready", ""}:
                    ready_by_region.setdefault(region, set()).add(plat)

        by_region = {
            reg: sorted(ready_by_region.get(reg) or plats)
            for reg, plats in sorted(all_by_region.items())
        }
        snapshot = {"nodes": by_node, "regions": by_region}
        with self._placement_lock:
            self._placement_cache = snapshot
            self._placement_cached_at = time.monotonic()
        return snapshot

    def infer_platforms_for_placement(
        self,
        *,
        nodes: list[str] | None = None,
        regions: list[str] | None = None,
        placement: dict[str, Any] | None = None,
    ) -> list[str]:
        snap = placement if isinstance(placement, dict) else self.cluster_placement_platforms()
        by_node = snap.get("nodes") if isinstance(snap.get("nodes"), dict) else {}
        by_region = snap.get("regions") if isinstance(snap.get("regions"), dict) else {}
        resolved: list[str] = []
        for n in nodes or []:
            plat = by_node.get(str(n or "").strip().lower())
            if plat and plat not in resolved:
                resolved.append(plat)
        for r in regions or []:
            for plat in by_region.get(str(r or "").strip().lower()) or []:
                if plat and plat not in resolved:
                    resolved.append(plat)
        return resolved

    def request_json(self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: int = 30) -> dict[str, Any]:
        if not self.configured:
            raise BuildError("LUMA_CONTROL_URL and LUMA_DEPLOY_TOKEN must be configured on luma-build for deployment")
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.endpoint}{path}",
            data=raw,
            method=method,
            headers={
                "Authorization": f"Bearer {self.deploy_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener().open(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return payload if isinstance(payload, dict) else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BuildError(f"Luma Control {path} returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise BuildError(f"Luma Control {path} failed: {exc}") from exc

    def deploy_built_artifact(
        self,
        *,
        build_record: dict[str, Any],
        env_secrets: dict[str, str] | None = None,
        on_step: Callable[[str], None] | None = None,
        timeout: int = 1800,
    ) -> dict[str, Any]:
        if not self.configured:
            raise BuildError("LUMA_CONTROL_URL and LUMA_DEPLOY_TOKEN are not configured on luma-build")

        request = build_record.get("request") if isinstance(build_record.get("request"), dict) else {}
        result = build_record.get("result") if isinstance(build_record.get("result"), dict) else {}
        kind = str(result.get("kind") or "service")
        manifest_text = str(result.get("manifest") or request.get("manifest") or "").strip()
        if not manifest_text:
            raise BuildError("no Luma deployment manifest (.luma.yml or luma.compose.yml) produced by build")
        authorized_apps = _authorized_app_names(build_record, request)

        source_name = f"{build_record.get('repoUrl')}@{result.get('commit') or build_record.get('ref')}"

        if kind == "compose":
            compose_content = str(result.get("composeContent") or "").strip()
            if not compose_content:
                raise BuildError("Compose build result is missing rewritten composeContent")
            sidecar = yaml.safe_load(manifest_text) or {}
            if not isinstance(sidecar, dict):
                raise BuildError("luma.compose.yml must be a YAML mapping")
            _validate_manifest_app_name(sidecar, authorized_apps)
            if request.get("region"):
                sidecar["region"] = request["region"]
            if request.get("node") and isinstance(sidecar.get("services"), dict):
                for svc in sidecar["services"].values():
                    if isinstance(svc, dict):
                        svc["node"] = request["node"]
            body: dict[str, Any] = {
                "manifest": yaml.safe_dump(sidecar, sort_keys=False, allow_unicode=False),
                "composeContent": compose_content,
                "sourceName": source_name,
            }
            if env_secrets:
                body["envSecrets"] = env_secrets
            return self._stream_or_post(
                stream_path="/v1/compose-deployments/stream",
                fallback_path="/v1/compose-deployments",
                body=body,
                on_step=on_step,
                timeout=timeout,
            )

        built_image = str(result.get("image") or "").strip()
        if not built_image:
            raise BuildError("service build result is missing built image reference")
        manifest_data = yaml.safe_load(manifest_text) or {}
        if not isinstance(manifest_data, dict):
            raise BuildError(".luma.yml must be a YAML mapping")
        _validate_manifest_app_name(manifest_data, authorized_apps)
        manifest_data.pop("build", None)
        manifest_data["image"] = built_image
        for override_key in ("region", "node", "exposure", "domain"):
            if request.get(override_key):
                manifest_data[override_key] = request[override_key]
        if request.get("port") is not None:
            manifest_data["port"] = int(request["port"])

        body = {
            "manifest": yaml.safe_dump(manifest_data, sort_keys=False, allow_unicode=False),
            "sourceName": source_name,
        }
        if env_secrets:
            body["envSecrets"] = env_secrets
        return self._stream_or_post(
            stream_path="/v1/deployments/stream",
            fallback_path="/v1/deployments",
            body=body,
            on_step=on_step,
            timeout=timeout,
        )

    def _stream_or_post(
        self,
        *,
        stream_path: str,
        fallback_path: str,
        body: dict[str, Any],
        on_step: Callable[[str], None] | None,
        timeout: int,
    ) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.endpoint}{stream_path}",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.deploy_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener().open(req, timeout=timeout) as resp:
                final_result: dict[str, Any] | None = None
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        continue
                    ev_status = str(event.get("status") or "")
                    if ev_status in {"start", "ok", "fail"}:
                        step_msg = f"[{ev_status}] {event.get('name') or 'deploy'}: {event.get('message') or ''}".strip()
                        if on_step:
                            on_step(step_msg)
                        if ev_status == "fail":
                            raise BuildError(str(event.get("message") or "Luma Control deployment failed"))
                    elif ev_status == "done" and isinstance(event.get("result"), dict):
                        final_result = event["result"]
                if final_result is not None:
                    return final_result
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return self.request_json("POST", fallback_path, body, timeout=timeout)
            detail = exc.read().decode("utf-8", errors="replace")
            raise BuildError(f"Luma Control {stream_path} returned HTTP {exc.code}: {detail}") from exc
        except BuildError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise BuildError(f"Luma Control stream {stream_path} interrupted: {exc}") from exc
        raise BuildError("Luma Control deployment stream ended without a final result")
