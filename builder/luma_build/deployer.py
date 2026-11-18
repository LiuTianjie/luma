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
import urllib.request
from typing import Any, Callable

import yaml

from .errors import BuildError
from .scheduler import parse_platforms


def extract_manifest_platforms(manifest_text: str) -> list[str]:
    """Extract explicit ``build.platforms`` / ``build.platform`` from an attached manifest."""
    if not manifest_text or not manifest_text.strip():
        return []
    try:
        data = yaml.safe_load(manifest_text)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    build_block = data.get("build") if isinstance(data.get("build"), dict) else {}
    raw_plat = build_block.get("platforms") or build_block.get("platform")
    return parse_platforms(raw_plat) if raw_plat else []


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

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.deploy_token)

    def _opener(self) -> urllib.request.OpenerDirector:
        return _standard_opener(insecure=self.insecure)

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
