"""Standard public-API client from luma-build to Luma Control.

``luma-build`` treats ``luma-control`` strictly as an external deployment target:
it sends only the built immutable ``image@sha256:...`` and deployment YAML to
``POST /v1/deployments/stream`` or ``POST /v1/compose-deployments/stream``.
No internal Control Python modules or database tables are shared.
"""
from __future__ import annotations

import hashlib
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

import yaml

from .errors import BuildError
from .scheduler import normalize_platform, parse_platforms


def extract_manifest_target_hints(manifest_text: str) -> tuple[list[str], str, str]:
    """Extract ``(explicit_platforms, region, node)`` from a Luma service or Compose manifest."""
    if not manifest_text or not manifest_text.strip():
        return [], "", ""
    try:
        data = yaml.safe_load(manifest_text)
    except yaml.YAMLError:
        return [], [], ""  # type: ignore[return-value]
    if not isinstance(data, dict):
        return [], "", ""

    build_block = data.get("build") if isinstance(data.get("build"), dict) else {}
    raw_plat = build_block.get("platforms") or build_block.get("platform")
    explicit_platforms = parse_platforms(raw_plat) if raw_plat else []

    region = str(data.get("region") or "").strip()
    node = str(data.get("node") or "").strip()
    services = data.get("services")
    if isinstance(services, dict):
        for svc in services.values():
            if not isinstance(svc, dict):
                continue
            if not node and svc.get("node"):
                node = str(svc["node"]).strip()
            if not region and svc.get("region"):
                region = str(svc["region"]).strip()
    return explicit_platforms, region, node


def probe_remote_manifest(
    *,
    repo_url: str,
    ref: str,
    compose_sidecar: str = "",
    git_auth: dict[str, str] | None = None,
    insecure: bool = False,
) -> str:
    """Fetch ONLY the ~1 KB Luma manifest via GitHub/Gitea HTTP API (zero ``git clone``!).

    Eliminates the legacy two-stage ``git clone`` where a planner node had to
    clone the entire repository just to inspect ``.luma.yml`` / ``luma.compose.yml``
    before routing the real build to an ARM or AMD64 runner.
    """
    if not repo_url.startswith(("https://", "http://")):
        return ""
    parsed = urllib.parse.urlsplit(repo_url)
    host = (parsed.netloc or "").lower()
    clean_path = parsed.path.strip("/")
    if clean_path.endswith(".git"):
        clean_path = clean_path[:-4]
    parts = [p for p in clean_path.split("/") if p]
    if not host or len(parts) < 2:
        return ""
    owner_repo = "/".join(urllib.parse.quote(p, safe="") for p in parts[-2:])
    encoded_ref = urllib.parse.quote(ref, safe="")

    candidates = (
        [compose_sidecar.strip().lstrip("/")]
        if compose_sidecar.strip()
        else ["luma.compose.yml", ".luma.yml", "luma.yml", ".luma.compose.yml"]
    )
    token = str((git_auth or {}).get("token") or "").strip()
    handlers: list[Any] = [_NoRedirectHandler()]
    if insecure:
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    opener = urllib.request.build_opener(*handlers)

    for rel_file in candidates:
        quoted_file = "/".join(urllib.parse.quote(seg, safe="") for seg in rel_file.split("/") if seg)
        if host == "github.com":
            api_url = f"https://api.github.com/repos/{owner_repo}/contents/{quoted_file}?ref={encoded_ref}"
            headers = {"Accept": "application/vnd.github.raw+json", "User-Agent": "luma-build"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
        else:
            api_url = f"{parsed.scheme}://{host}/api/v1/repos/{owner_repo}/raw/{quoted_file}?ref={encoded_ref}"
            headers = {"Accept": "text/plain", "User-Agent": "luma-build"}
            if token:
                headers["Authorization"] = f"token {token}"

        req = urllib.request.Request(api_url, method="GET", headers=headers)
        try:
            with opener.open(req, timeout=4) as resp:
                if int(resp.status) == 200:
                    body = resp.read(65536).decode("utf-8", errors="replace")
                    if body.strip():
                        return body
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            continue
    return ""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(req.full_url, code, f"unexpected redirect ({code})", headers, fp)


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
        handlers: list[Any] = [_NoRedirectHandler()]
        if self.insecure:
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        return urllib.request.build_opener(*handlers)

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

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.deploy_token)

    def _opener(self) -> urllib.request.OpenerDirector:
        handlers: list[Any] = [_NoRedirectHandler()]
        if self.insecure:
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        return urllib.request.build_opener(*handlers)

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

    def infer_target_platforms(self, *, region: str = "", node: str = "") -> list[str]:
        """Optionally query ``GET /v1/status`` to infer target node architecture."""
        if not self.configured:
            return ["linux/amd64"]
        status = self.request_json("GET", "/v1/status", timeout=15)
        raw_nodes = status.get("nodes")
        if isinstance(raw_nodes, dict):
            iterable = [{"name": k, **(v if isinstance(v, dict) else {})} for k, v in raw_nodes.items()]
        elif isinstance(raw_nodes, list):
            iterable = [item for item in raw_nodes if isinstance(item, dict)]
        else:
            iterable = []
        matched: list[str] = []
        for item in iterable:
            item_name = str(item.get("name") or "")
            if node and item_name != node:
                continue
            if not node and region and str(item.get("region") or "") != region:
                continue
            native_plat = str(item.get("nativePlatform") or "").strip()
            if native_plat:
                norm = normalize_platform(native_plat)
            else:
                arch = str(item.get("arch") or (item.get("agent") or {}).get("arch") or "").strip()
                norm = normalize_platform(f"linux/{arch}") if arch else ""
            if norm and norm not in matched:
                matched.append(norm)
        return matched or ["linux/amd64"]

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

        source_name = f"{build_record.get('repoUrl')}@{result.get('commit') or build_record.get('ref')}"

        if kind == "compose":
            compose_content = str(result.get("composeContent") or "").strip()
            if not compose_content:
                raise BuildError("Compose build result is missing rewritten composeContent")
            sidecar = yaml.safe_load(manifest_text) or {}
            if not isinstance(sidecar, dict):
                raise BuildError("luma.compose.yml must be a YAML mapping")
            if request.get("region"):
                sidecar["region"] = request["region"]
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
