from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Sequence

VERSION = "0.1.0"


def component_payload(component: str, status: str = "ok", **extra: Any) -> dict[str, Any]:
    return {
        "schemaVersion": "lae.component-status/v1",
        "component": component,
        "status": status,
        "version": VERSION,
        **extra,
    }


def emit_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def _serve(component: str, host: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path in {"/health/live", "/health/ready"}:
                body = component_payload(component)
                status = 200
            elif self.path == "/version":
                body = component_payload(component, status="version")
                status = 200
            else:
                body = {
                    "schemaVersion": "lae.error/v1",
                    "code": "LAE_NOT_FOUND",
                    "message": "Route not found",
                    "requestId": "req_health_server",
                    "retryable": False,
                    "details": {"path": self.path},
                }
                status = 404
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()


def run_component(
    component: str,
    argv: Sequence[str] | None = None,
    *,
    default_port: int | None = None,
    once_event: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=component)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--health", action="store_true", help="Print component health as JSON")
    action.add_argument("--version", action="store_true", help="Print component version as JSON")
    if default_port is not None:
        action.add_argument("--serve", action="store_true", help="Serve health and version endpoints")
        parser.add_argument("--host", default="0.0.0.0")
        parser.add_argument("--port", type=int, default=default_port)
    if once_event is not None:
        action.add_argument("--once", action="store_true", help="Run one non-blocking worker iteration")
    args = parser.parse_args(argv)

    if getattr(args, "serve", False):
        _serve(component, args.host, args.port)
        return 0
    if getattr(args, "once", False):
        emit_json(component_payload(component, event=once_event))
        return 0
    if args.version:
        emit_json(component_payload(component, status="version"))
        return 0
    emit_json(component_payload(component))
    return 0
