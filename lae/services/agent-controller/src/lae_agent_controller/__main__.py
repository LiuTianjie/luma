from __future__ import annotations

import argparse
from collections import deque
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, Sequence

from lae_agent_core import (
    AIDiagnosticError,
    OpenAICompatibleConfig,
    authorized,
    call_openai_compatible,
    canonical_bytes,
    load_knowledge_pack,
)
from lae_core import VERSION, component_payload, emit_json
from lae_contracts import validate_instance

_MAX_REQUEST_BYTES = 256 * 1024
_MAX_CONCURRENT = 4


def _error(code: str, *, retryable: bool) -> dict[str, Any]:
    return {
        "schemaVersion": "lae.agent-controller-error/v1",
        "code": code,
        "message": "AI deployment diagnostic is unavailable",
        "retryable": retryable,
    }


def _valid_request(value: object, *, knowledge_version: str) -> bool:
    if not isinstance(value, Mapping) or value.get("schemaVersion") != "lae.ai-analysis-request/v1":
        return False
    if set(value) != {"schemaVersion", "source", "deterministic", "expectedOutput"}:
        return False
    source = value.get("source")
    deterministic = value.get("deterministic")
    expected = value.get("expectedOutput")
    if (
        not isinstance(source, Mapping)
        or set(source) != {"digest", "kind", "files"}
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(source.get("digest") or ""))
        or source.get("kind") not in {"service", "compose"}
        or not isinstance(source.get("files"), list)
        or len(source["files"]) > 16
    ):
        return False
    for item in source["files"]:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"path"}
            or not isinstance(item.get("path"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@+-]{0,511}", item["path"])
            or any(part in {".env", ".."} for part in item["path"].split("/"))
            or re.search(r"(?:credential|password|private.?key|secret|token)", item["path"], re.I)
        ):
            return False
    if not isinstance(deterministic, Mapping) or set(deterministic) != {
        "deploymentPlan",
        "buildPlan",
        "findings",
    }:
        return False
    deployment_plan = deterministic.get("deploymentPlan")
    build_plan = deterministic.get("buildPlan")
    findings = deterministic.get("findings")
    if (
        not isinstance(deployment_plan, Mapping)
        or validate_instance("deployment-plan.v1.schema.json", deployment_plan)
        or not isinstance(build_plan, Mapping)
        or validate_instance("build-plan-proposal.v1.schema.json", build_plan)
        or not isinstance(findings, list)
        or len(findings) > 4096
    ):
        return False
    finding_keys = {"path", "rule", "count", "name", "field", "line", "role"}
    if any(
        not isinstance(item, Mapping)
        or not set(item) <= finding_keys
        or any(not isinstance(key, str) for key in item)
        or any(not isinstance(item[key], (str, int)) for key in item)
        for item in findings
    ):
        return False
    return bool(
        isinstance(expected, Mapping)
        and set(expected) == {"deploymentPlan", "manifestCandidate", "knowledgeVersion"}
        and expected.get("deploymentPlan") == "lae.deployment-plan/v1"
        and expected.get("manifestCandidate") == "lae.luma-manifest-candidate/v1"
        and expected.get("knowledgeVersion") == knowledge_version
    )


def _serve(host: str, port: int) -> None:
    token = os.environ.get("LAE_AGENT_CONTROLLER_TOKEN", "").strip()
    try:
        provider = OpenAICompatibleConfig.from_env()
        provider_error: str | None = None
    except AIDiagnosticError as exc:
        provider = None
        provider_error = exc.code
    try:
        knowledge = load_knowledge_pack(
            os.environ.get(
                "LAE_AGENT_KNOWLEDGE_PATH",
                "/opt/lae/knowledge/v1/knowledge-pack.json",
            )
        )
        knowledge_error: str | None = None
    except AIDiagnosticError as exc:
        knowledge = None
        knowledge_error = exc.code
    semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT)
    rate_lock = threading.Lock()
    rate_window: deque[float] = deque()
    circuit = {"failures": 0, "openUntil": 0.0}
    ai_required = os.environ.get("LAE_AGENT_AI_REQUIRED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path not in {"/health/live", "/health/ready", "/version"}:
                self._write(404, _error("LAE_AGENT_ROUTE_NOT_FOUND", retryable=False))
                return
            status = "version" if self.path == "/version" else "ok"
            configured = provider is not None and knowledge is not None and bool(token)
            self._write(
                503
                if self.path == "/health/ready" and ai_required and not configured
                else 200,
                component_payload(
                    "lae-agent-controller",
                    status=status,
                    ai={
                        "configured": configured,
                        "mode": "ai" if configured else "deterministic_fallback",
                        "configurationError": provider_error or knowledge_error,
                    },
                ),
            )

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/analyze":
                self._write(404, _error("LAE_AGENT_ROUTE_NOT_FOUND", retryable=False))
                return
            if not token or not authorized(token, self.headers.get("Authorization")):
                self._write(401, _error("LAE_AGENT_UNAUTHORIZED", retryable=False))
                return
            if provider is None or knowledge is None:
                self._write(503, _error(provider_error or knowledge_error or "AI_PROVIDER_NOT_CONFIGURED", retryable=True))
                return
            now = time.monotonic()
            with rate_lock:
                while rate_window and rate_window[0] <= now - 60:
                    rate_window.popleft()
                if circuit["openUntil"] > now:
                    self._write(503, _error("AI_PROVIDER_CIRCUIT_OPEN", retryable=True))
                    return
                if len(rate_window) >= 30:
                    self._write(429, _error("AI_CONTROLLER_RATE_LIMITED", retryable=True))
                    return
                rate_window.append(now)
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if not 0 <= length <= _MAX_REQUEST_BYTES:
                self._write(413, _error("AI_REQUEST_TOO_LARGE", retryable=False))
                return
            if not semaphore.acquire(blocking=False):
                self._write(429, _error("AI_CONTROLLER_BUSY", retryable=True))
                return
            try:
                try:
                    value = json.loads(self.rfile.read(length))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._write(400, _error("AI_REQUEST_INVALID", retryable=False))
                    return
                if not _valid_request(value, knowledge_version=knowledge["knowledgeVersion"]):
                    self._write(400, _error("AI_REQUEST_INVALID", retryable=False))
                    return
                try:
                    proposal = call_openai_compatible(provider, value, knowledge)
                except AIDiagnosticError as exc:
                    with rate_lock:
                        circuit["failures"] += 1
                        if circuit["failures"] >= 5:
                            circuit["openUntil"] = time.monotonic() + 60
                    self._write(502, _error(exc.code, retryable=True))
                    return
                with rate_lock:
                    circuit["failures"] = 0
                    circuit["openUntil"] = 0.0
                self._write(
                    200,
                    {
                        "schemaVersion": "lae.ai-analysis-response/v1",
                        "status": "succeeded",
                        "model": provider.model,
                        "knowledgeVersion": knowledge["knowledgeVersion"],
                        "proposal": proposal,
                    },
                )
            finally:
                semaphore.release()

        def _write(self, status: int, value: Mapping[str, Any]) -> None:
            encoded = canonical_bytes(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lae-agent-controller")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--health", action="store_true")
    action.add_argument("--version", action="store_true")
    action.add_argument("--serve", action="store_true")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args(argv)
    if args.serve:
        _serve(args.host, args.port)
        return 0
    emit_json(component_payload("lae-agent-controller", status="version" if args.version else "ok"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
