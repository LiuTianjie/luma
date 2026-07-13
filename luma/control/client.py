from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, BinaryIO, Dict, Iterator

from ..errors import LumaError
from ..repo_paths import normalize_repo_relative_path


class ControlClient:
    def __init__(self, endpoint: str, token: str, *, insecure: bool = False, resolve_ip: str | None = None):
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme != "https":
            raise LumaError("control endpoint must use https")
        if not parsed.hostname:
            raise LumaError("control endpoint must include a hostname")
        if resolve_ip and not insecure:
            raise LumaError("--resolve-ip requires --insecure; use DNS for verified TLS")
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.insecure = insecure
        self.resolve_ip = resolve_ip
        self._host_header = parsed.netloc

    def request(
        self,
        method: str,
        path: str,
        body: Dict[str, Any] | None = None,
        *,
        timeout: int = 30,
        headers: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        with self._open(method, path, body, timeout=timeout, headers=headers) as response:
            raw = response.read().decode("utf-8", errors="replace")
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise LumaError(
                "control API returned a non-JSON response "
                "(is the endpoint pointed at the Luma control plane?)"
            ) from exc
        if not isinstance(payload, dict):
            raise LumaError("control API returned invalid JSON")
        return payload

    def stream(
        self,
        method: str,
        path: str,
        body: Dict[str, Any] | None = None,
        *,
        timeout: int = 30,
        headers: Dict[str, str] | None = None,
    ) -> Iterator[Dict[str, Any]]:
        response = self._open(method, path, body, timeout=timeout, headers=headers)
        with response:
            line_iter = iter(response)
            while True:
                try:
                    raw_line = next(line_iter)
                except StopIteration:
                    break
                except (TimeoutError, socket.timeout) as exc:
                    raise LumaError(_timeout_message(path, timeout)) from exc
                except (urllib.error.URLError, OSError) as exc:
                    # A mid-stream connection drop / read timeout must surface as
                    # a clean LumaError, not a raw traceback the CLI can't format.
                    raise LumaError(f"control API stream interrupted: {exc}") from exc
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError as exc:
                    raise LumaError(
                        "control API returned an invalid stream line "
                        "(is the endpoint pointed at the Luma control plane?)"
                    ) from exc
                if not isinstance(payload, dict):
                    raise LumaError("control API returned invalid stream JSON")
                yield payload

    def _open(
        self,
        method: str,
        path: str,
        body: Dict[str, Any] | None = None,
        *,
        timeout: int = 30,
        headers: Dict[str, str] | None = None,
    ) -> Any:
        data = json.dumps(body or {}).encode("utf-8") if body is not None else None
        request_headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        for name, value in (headers or {}).items():
            normalized_name = str(name or "").strip()
            if not normalized_name or normalized_name.lower() in {"authorization", "host", "content-type"}:
                raise LumaError(f"control request header cannot be overridden: {normalized_name or '<empty>'}")
            request_headers[normalized_name] = str(value)
        if self.resolve_ip:
            request_headers["Host"] = self._host_header
        req = urllib.request.Request(
            self._request_url(path),
            data=data,
            method=method,
            headers=request_headers,
        )
        try:
            kwargs: Dict[str, Any] = {"timeout": timeout}
            if self.insecure:
                kwargs["context"] = ssl._create_unverified_context()
            return urllib.request.urlopen(req, **kwargs)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 404 and path == "/v1/nodes/agent-token":
                raise LumaError(
                    "control API does not support node-agent credentials yet. "
                    "Update the manager control plane first: run `luma update manager` on the manager."
                ) from exc
            raise LumaError(f"control API error {exc.code}: {detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LumaError(_timeout_message(path, timeout)) from exc
        except urllib.error.URLError as exc:
            raise LumaError(f"control API unavailable: {exc}") from exc

    def _request_url(self, path: str) -> str:
        if not self.resolve_ip:
            return self.endpoint + path
        parsed = urllib.parse.urlparse(self.endpoint)
        port = f":{parsed.port}" if parsed.port else ""
        netloc = f"{self.resolve_ip}{port}"
        return urllib.parse.urlunparse((parsed.scheme, netloc, path, "", "", ""))

    def verify_login(self) -> Dict[str, Any]:
        return self.request("POST", "/v1/auth/login/verify", {})

    def health(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/health")

    def status(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/status")

    def create_builder_task(
        self,
        request_body: Dict[str, Any],
        *,
        idempotency_key: str,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        key = str(idempotency_key or "").strip()
        if not key:
            raise LumaError("idempotency_key is required")
        return self.request(
            "POST",
            "/v1/builder/tasks",
            dict(request_body),
            timeout=timeout,
            headers={"Idempotency-Key": key},
        )

    def get_builder_task(self, task_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/v1/builder/tasks/{urllib.parse.quote(str(task_id), safe='')}")

    def get_builder_task_events(self, task_id: str, *, after: int = 0, limit: int = 200) -> Dict[str, Any]:
        query = urllib.parse.urlencode({"after": int(after), "limit": int(limit)})
        task_path = urllib.parse.quote(str(task_id), safe="")
        return self.request("GET", f"/v1/builder/tasks/{task_path}/events?{query}")

    def cancel_builder_task(self, task_id: str) -> Dict[str, Any]:
        task_path = urllib.parse.quote(str(task_id), safe="")
        return self.request("POST", f"/v1/builder/tasks/{task_path}/cancel", {})

    def upload_builder_artifact(
        self,
        *,
        lease_id: str,
        node_name: str,
        node_id: str,
        stream: BinaryIO,
        media_type: str,
        digest: str,
        size_bytes: int,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        """Stream a verified builder artifact to an in-memory Control lease."""

        if not lease_id or not node_name or size_bytes <= 0:
            raise LumaError("builder artifact upload binding is invalid")

        def chunks() -> Iterator[bytes]:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    return
                yield chunk

        path = (
            "/v1/node-agent/artifact-downloads/"
            f"{urllib.parse.quote(lease_id, safe='')}/content"
        )
        request_headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": media_type,
            "Content-Length": str(size_bytes),
            "X-Luma-Artifact-Digest": digest,
            "X-Luma-Node-Name": node_name,
            "X-Luma-Node-Id": node_id,
            "Accept": "application/json",
        }
        if self.resolve_ip:
            request_headers["Host"] = self._host_header
        request = urllib.request.Request(
            self._request_url(path),
            data=chunks(),
            method="POST",
            headers=request_headers,
        )

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args: object, **_kwargs: object) -> None:
                return None

        context = (
            ssl._create_unverified_context()
            if self.insecure
            else ssl.create_default_context()
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPSHandler(context=context),
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(64 * 1024 + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            raise LumaError("builder artifact upload failed") from exc
        if len(raw) > 64 * 1024:
            raise LumaError("builder artifact upload returned an invalid response")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise LumaError(
                "builder artifact upload returned an invalid response"
            ) from exc
        if not isinstance(value, dict):
            raise LumaError("builder artifact upload returned an invalid response")
        return value

    def restart_application(self, *, stack: str, service: str = "", mode: str = "", timeout: int = 120) -> Dict[str, Any]:
        body: Dict[str, Any] = {"stack": stack, "service": service, "mode": mode}
        return self.request("POST", "/v1/applications/restart", body, timeout=timeout)

    def register_node(self, *, node_name: str, region: str) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/v1/nodes/register",
            {"nodeName": node_name, "region": region},
        )

    def label_node(
        self,
        *,
        node_name: str,
        region: str,
        registered_name: str | None = None,
        node_id: str | None = None,
        tailscale_ip: str | None = None,
        tailscale_name: str | None = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"nodeName": node_name, "region": region}
        if registered_name and registered_name != node_name:
            body["registeredName"] = registered_name
        if node_id:
            body["nodeId"] = node_id
        if tailscale_ip:
            body["tailscaleIP"] = tailscale_ip
        if tailscale_name:
            body["tailscaleName"] = tailscale_name
        return self.request("POST", "/v1/nodes/label", body, timeout=120)

    def unregister_node(self, *, node_name: str) -> Dict[str, Any]:
        return self.request("POST", "/v1/nodes/unregister", {"nodeName": node_name})

    def join_nomad_node(
        self,
        *,
        node_name: str,
        region: str | None = None,
        server_addr: str | None = None,
        timeout: int = 1200,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"nodeName": node_name, "timeout": timeout}
        if region:
            body["region"] = region
        if server_addr:
            body["serverAddr"] = server_addr
        return self.request("POST", "/v1/nodes/nomad-join", body, timeout=max(int(timeout), 60) + 30)

    def issue_agent_token(self, *, node_name: str, node_id: str | None = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"nodeName": node_name}
        if node_id:
            body["nodeId"] = node_id
        return self.request("POST", "/v1/nodes/agent-token", body)

    def lease_agent_task(
        self,
        *,
        node_name: str,
        node_id: str = "",
        os_name: str = "",
        arch: str = "",
        version: str = "",
        capabilities: list[str] | None = None,
        metrics: Dict[str, Any] | None = None,
        container_stats: list[Dict[str, Any]] | None = None,
        diagnostics: Dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "nodeName": node_name,
            "nodeId": node_id,
            "os": os_name,
            "arch": arch,
            "version": version,
            "capabilities": capabilities or [],
            "waitSeconds": max(timeout - 5, 1),
        }
        if metrics:
            body["metrics"] = metrics
        if container_stats is not None:
            body["containerStats"] = container_stats
        if diagnostics is not None:
            body["diagnostics"] = diagnostics
        return self.request(
            "POST",
            "/v1/node-agent/lease",
            body,
            timeout=timeout,
        )

    def heartbeat_agent(
        self,
        *,
        node_name: str,
        node_id: str = "",
        active_task_id: str = "",
        os_name: str = "",
        arch: str = "",
        version: str = "",
        capabilities: list[str] | None = None,
        metrics: Dict[str, Any] | None = None,
        container_stats: list[Dict[str, Any]] | None = None,
        diagnostics: Dict[str, Any] | None = None,
        timeout: int = 15,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "nodeName": node_name,
            "nodeId": node_id,
            "os": os_name,
            "arch": arch,
            "version": version,
            "capabilities": capabilities or [],
        }
        if active_task_id:
            body["activeTaskId"] = active_task_id
        if metrics:
            body["metrics"] = metrics
        if container_stats is not None:
            body["containerStats"] = container_stats
        if diagnostics is not None:
            body["diagnostics"] = diagnostics
        return self.request(
            "POST",
            "/v1/node-agent/heartbeat",
            body,
            timeout=timeout,
        )

    def complete_agent_task(
        self,
        *,
        task_id: str,
        node_name: str,
        node_id: str = "",
        status: str,
        message: str = "",
        result: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/v1/node-agent/tasks/complete",
            {
                "taskId": task_id,
                "nodeName": node_name,
                "nodeId": node_id,
                "status": status,
                "message": message,
                "result": result or {},
            },
        )

    def progress_agent_task(
        self,
        *,
        task_id: str,
        node_name: str,
        node_id: str = "",
        events: list[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/v1/node-agent/tasks/progress",
            {
                "taskId": task_id,
                "nodeName": node_name,
                "nodeId": node_id,
                "events": events or [],
            },
        )

    def update_fleet(self, *, install_ref: str = "", include_all: bool = False, include_manager: bool = False, timeout: int = 900) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/v1/fleet/update",
            {
                "installRef": install_ref,
                "includeAll": include_all,
                "includeManager": include_manager,
                "timeout": timeout,
            },
            timeout=max(int(timeout), 60) * 20,
        )

    def deploy(
        self,
        *,
        manifest: str,
        source_name: str,
        skip_dns: bool = False,
        skip_orchestrator: bool = False,
        env_secrets: Dict[str, str] | None = None,
        timeout: int = 1800,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "manifest": manifest,
            "sourceName": source_name,
            "skipDns": skip_dns,
            "skipOrchestrator": skip_orchestrator,
        }
        if env_secrets is not None:
            body["envSecrets"] = env_secrets
        return self.request(
            "POST",
            "/v1/deployments",
            body,
            timeout=timeout,
        )

    def deploy_events(
        self,
        *,
        manifest: str,
        source_name: str,
        skip_dns: bool = False,
        skip_orchestrator: bool = False,
        env_secrets: Dict[str, str] | None = None,
        timeout: int = 1800,
    ) -> Iterator[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "manifest": manifest,
            "sourceName": source_name,
            "skipDns": skip_dns,
            "skipOrchestrator": skip_orchestrator,
        }
        if env_secrets is not None:
            body["envSecrets"] = env_secrets
        return self.stream(
            "POST",
            "/v1/deployments/stream",
            body,
            timeout=timeout,
        )

    def deploy_compose(
        self,
        *,
        manifest: str,
        compose_content: str,
        source_name: str,
        skip_dns: bool = False,
        skip_orchestrator: bool = False,
        env_secrets: Dict[str, str] | None = None,
        timeout: int = 1800,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "manifest": manifest,
            "composeContent": compose_content,
            "sourceName": source_name,
            "skipDns": skip_dns,
            "skipOrchestrator": skip_orchestrator,
        }
        if env_secrets is not None:
            body["envSecrets"] = env_secrets
        return self.request(
            "POST",
            "/v1/compose-deployments",
            body,
            timeout=timeout,
        )

    def deploy_compose_events(
        self,
        *,
        manifest: str,
        compose_content: str,
        source_name: str,
        skip_dns: bool = False,
        skip_orchestrator: bool = False,
        env_secrets: Dict[str, str] | None = None,
        timeout: int = 1800,
    ) -> Iterator[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "manifest": manifest,
            "composeContent": compose_content,
            "sourceName": source_name,
            "skipDns": skip_dns,
            "skipOrchestrator": skip_orchestrator,
        }
        if env_secrets is not None:
            body["envSecrets"] = env_secrets
        return self.stream(
            "POST",
            "/v1/compose-deployments/stream",
            body,
            timeout=timeout,
        )

    def build_deploy(
        self,
        *,
        repo_url: str = "",
        build_node: str = "",
        provider_id: str = "",
        repository: str = "",
        ref: str = "",
        region: str = "",
        exposure: str = "",
        domain: str = "",
        port: int | None = None,
        platform: str = "",
        context: str = "",
        dockerfile: str = "",
        registry_host: str = "",
        proxy_mode: str = "auto",
        manifest: str = "",
        compose_sidecar: str = "",
        env_secrets: Dict[str, str] | None = None,
        timeout: int = 2400,
    ) -> Dict[str, Any]:
        if proxy_mode == "direct":
            self._require_build_proxy_mode()
        if compose_sidecar:
            compose_sidecar = normalize_repo_relative_path(
                compose_sidecar, label="composeSidecar"
            )
            self._require_repository_compose_sidecar()
        return self.request("POST", "/v1/builds", self._build_body(locals()), timeout=timeout)

    def build_deploy_events(
        self,
        *,
        repo_url: str = "",
        build_node: str = "",
        provider_id: str = "",
        repository: str = "",
        ref: str = "",
        region: str = "",
        exposure: str = "",
        domain: str = "",
        port: int | None = None,
        platform: str = "",
        context: str = "",
        dockerfile: str = "",
        registry_host: str = "",
        proxy_mode: str = "auto",
        manifest: str = "",
        compose_sidecar: str = "",
        env_secrets: Dict[str, str] | None = None,
        timeout: int = 2400,
    ) -> Iterator[Dict[str, Any]]:
        if proxy_mode == "direct":
            self._require_build_proxy_mode()
        if compose_sidecar:
            compose_sidecar = normalize_repo_relative_path(
                compose_sidecar, label="composeSidecar"
            )
            self._require_repository_compose_sidecar()
        return self.stream("POST", "/v1/builds/stream", self._build_body(locals()), timeout=timeout)

    def _require_repository_compose_sidecar(self) -> None:
        capabilities = self.health().get("capabilities") or []
        if not isinstance(capabilities, list) or "repository-compose-sidecar-v1" not in {
            str(item) for item in capabilities
        }:
            raise LumaError(
                "Luma Control does not support explicit Compose sidecar selection; "
                "update the manager before running this import"
            )

    def _require_build_proxy_mode(self) -> None:
        capabilities = self.health().get("capabilities") or []
        if not isinstance(capabilities, list) or "build-proxy-mode-v1" not in {
            str(item) for item in capabilities
        }:
            raise LumaError(
                "Luma Control does not support explicit direct builder networking; "
                "update the manager before using --proxy-mode direct"
            )

    @staticmethod
    def _build_body(values: Dict[str, Any]) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if values.get("repo_url"):
            body["repoUrl"] = values["repo_url"]
        if values.get("build_node"):
            body["buildNode"] = values["build_node"]
        if values.get("provider_id"):
            body["providerId"] = values["provider_id"]
        if values.get("repository"):
            body["repository"] = values["repository"]
        optional = {
            "ref": values.get("ref"),
            "region": values.get("region"),
            "exposure": values.get("exposure"),
            "domain": values.get("domain"),
            "platform": values.get("platform"),
            "context": values.get("context"),
            "dockerfile": values.get("dockerfile"),
            "registryHost": values.get("registry_host"),
            "manifest": values.get("manifest"),
            "composeSidecar": values.get("compose_sidecar"),
        }
        for key, value in optional.items():
            if value:
                body[key] = value
        proxy_mode = str(values.get("proxy_mode") or "auto").strip().lower()
        if proxy_mode not in {"auto", "direct"}:
            raise LumaError("proxy mode must be auto or direct")
        if proxy_mode == "direct":
            # Keep both representations so upgraded Control can retain the
            # user's intent and can distinguish explicit empty from omission.
            body["proxyMode"] = "direct"
            body["proxy"] = ""
        if values.get("port"):
            body["port"] = int(values["port"])
        if values.get("env_secrets") is not None:
            body["envSecrets"] = values["env_secrets"]
        return body

    def list_builds(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/builds")

    def get_build(self, build_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/v1/builds/{urllib.parse.quote(build_id, safe='')}")

    def retry_build(self, build_id: str, *, timeout: int = 2400, env_secrets: Dict[str, str] | None = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if env_secrets is not None:
            body["envSecrets"] = env_secrets
        return self.request("POST", f"/v1/builds/{urllib.parse.quote(build_id, safe='')}/retry", body, timeout=timeout)

    def cancel_build(self, build_id: str) -> Dict[str, Any]:
        return self.request(
            "POST",
            f"/v1/builds/{urllib.parse.quote(build_id, safe='')}/cancel",
            {},
        )

    def configure_build(
        self,
        *,
        node: str = "",
        nodes: list[str] | None = None,
        default_node: str = "",
        registry_host: str = "",
        push_host: str = "",
        direct_egress_nodes: list[str] | None = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if node:
            body["node"] = node
        if nodes is not None:
            body["nodes"] = nodes
        if default_node:
            body["defaultNode"] = default_node
        if registry_host:
            body["registryHost"] = registry_host
        if push_host:
            body["pushHost"] = push_host
        if direct_egress_nodes is not None:
            body["directEgressNodes"] = direct_egress_nodes
        return self.request("POST", "/v1/builds/config", body)

    def registry_serve(
        self,
        *,
        node: str,
        port: int = 5000,
        image: str = "",
        name: str = "",
        storage_class: str = "",
        domain: str = "",
        username: str = "",
        password: str = "",
        activate: bool = True,
        timeout: int = 1800,
    ) -> Dict[str, Any]:
        return self.request("POST", "/v1/registry/serve", self._registry_serve_body(locals()), timeout=timeout)

    def registry_serve_events(
        self,
        *,
        node: str,
        port: int = 5000,
        image: str = "",
        name: str = "",
        storage_class: str = "",
        domain: str = "",
        username: str = "",
        password: str = "",
        activate: bool = True,
        timeout: int = 1800,
    ) -> Iterator[Dict[str, Any]]:
        return self.stream("POST", "/v1/registry/serve/stream", self._registry_serve_body(locals()), timeout=timeout)

    @staticmethod
    def _registry_serve_body(values: Dict[str, Any]) -> Dict[str, Any]:
        body: Dict[str, Any] = {"node": values["node"], "port": int(values.get("port") or 5000)}
        for src, dst in (
            ("image", "image"),
            ("name", "name"),
            ("storage_class", "storageClass"),
            ("domain", "domain"),
            ("username", "username"),
            ("password", "password"),
        ):
            if values.get(src):
                body[dst] = str(values[src])
        body["activate"] = bool(values.get("activate", True))
        return body

    def remove_service(
        self,
        *,
        name: str,
        skip_dns: bool = False,
        skip_orchestrator: bool = False,
        delete_storage: bool = False,
        dry_run: bool = False,
        timeout: int = 300,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "name": name,
            "skipDns": skip_dns,
            "skipOrchestrator": skip_orchestrator,
            "deleteStorage": delete_storage,
            "dryRun": dry_run,
        }
        return self.request("POST", "/v1/services/remove", body, timeout=timeout)

    def rollback_service(
        self,
        *,
        name: str,
        version: int | None = None,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"name": name}
        if version is not None:
            body["version"] = version
        return self.request("POST", "/v1/services/rollback", body, timeout=timeout)

    def service_history(self, *, name: str, timeout: int = 60) -> Dict[str, Any]:
        return self.request("POST", "/v1/services/history", {"name": name}, timeout=timeout)

    def apply_storage(
        self,
        *,
        manifest: str,
        compose_content: str,
        source_name: str,
        timeout: int = 300,
    ) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/v1/storage/apply",
            {"manifest": manifest, "composeContent": compose_content, "sourceName": source_name},
            timeout=timeout,
        )

    def list_secrets(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/secrets")

    def set_secret(self, *, name: str, value: str, scope: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"name": name, "value": value}
        if scope:
            body["scope"] = scope
        return self.request("POST", "/v1/secrets", body)

    def list_registries(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/registries")

    def set_registry(self, *, host: str, username: str, password: str) -> Dict[str, Any]:
        return self.request("POST", "/v1/registries", {"host": host, "username": username, "password": password})

    def remove_registry(self, *, host: str) -> Dict[str, Any]:
        return self.request("POST", "/v1/registries/remove", {"host": host})

    def list_git_providers(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/git-providers")

    def set_git_provider(
        self,
        *,
        provider_type: str,
        account: str,
        token: str,
        base_url: str = "",
        clone_base_url: str = "",
        username: str = "",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"type": provider_type, "account": account, "token": token}
        if base_url:
            body["baseUrl"] = base_url
        if clone_base_url:
            body["cloneBaseUrl"] = clone_base_url
        if username:
            body["username"] = username
        return self.request("POST", "/v1/git-providers", body)

    def remove_git_provider(self, *, provider_id: str) -> Dict[str, Any]:
        return self.request("POST", "/v1/git-providers/remove", {"id": provider_id})

    def list_git_provider_repositories(self, *, provider_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/v1/git-providers/{urllib.parse.quote(provider_id, safe='')}/repositories")

    def list_git_provider_refs(self, *, provider_id: str, repository: str) -> Dict[str, Any]:
        owner, _, repo = repository.partition("/")
        if not owner or not repo:
            raise LumaError("repository must be owner/repo")
        return self.request(
            "GET",
            f"/v1/git-providers/{urllib.parse.quote(provider_id, safe='')}/repositories/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}/refs",
        )

    def list_storage(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/storage")

    def set_storage(
        self,
        *,
        name: str,
        provider: str,
        external: bool = False,
        node: str = "",
        path: str = "",
        endpoint: str = "",
        mount_options: str = "",
        regions: list[str] | None = None,
        nodes: list[str] | None = None,
    ) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/v1/storage",
            {
                "name": name,
                "provider": provider,
                "external": external,
                "node": node,
                "path": path,
                "endpoint": endpoint,
                "mountOptions": mount_options,
                "regions": regions or [],
                "nodes": nodes or [],
            },
        )

    def remove_storage(self, *, name: str) -> Dict[str, Any]:
        return self.request("POST", "/v1/storage/remove", {"name": name})

def _timeout_message(path: str, timeout: int) -> str:
    message = f"control API timed out after {timeout}s waiting for {path}"
    if path == "/v1/deployments":
        message += (
            "; the manager may still be applying the deployment. "
            "Check `nomad job status <service>` and luma-control allocation logs, "
            "or retry with `luma deploy <service.yaml> --timeout 3600`."
        )
    return message
