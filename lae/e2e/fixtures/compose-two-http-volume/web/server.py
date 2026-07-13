from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


STATE = Path("/data/requests.txt")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path == "/healthz":
            self._send(200, {"status": "ok", "service": "web"})
            return
        try:
            count = int(STATE.read_text(encoding="utf-8") or "0") + 1
        except (OSError, ValueError):
            count = 1
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(str(count), encoding="utf-8")
        self._send(200, {"service": "web", "persistentRequestCount": count})

    def _send(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

