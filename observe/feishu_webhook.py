#!/usr/bin/env python3
"""Receive Alertmanager webhooks and post Feishu text messages.

Supports a custom-bot webhook URL. App-id bots are optional and use the same
tenant-token protocol as Luma Control. Missing credentials return 204 so
Alertmanager does not retry a misconfigured first deploy.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def format_text(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "unknown")
    alerts = payload.get("alerts") if isinstance(payload.get("alerts"), list) else []
    title = "Luma Observe 告警触发" if status == "firing" else "Luma Observe 告警恢复"
    lines = [title]
    for alert in alerts[:8]:
        if not isinstance(alert, dict):
            continue
        labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
        annotations = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
        name = str(labels.get("alertname") or "alert")
        summary = str(annotations.get("summary") or annotations.get("description") or "")
        target = str(labels.get("router") or labels.get("job") or labels.get("service") or "")
        bits = [name]
        if target:
            bits.append(target)
        if summary:
            bits.append(summary)
        lines.append(" · ".join(bits))
    if len(alerts) > 8:
        lines.append(f"另有 {len(alerts) - 8} 条未展开")
    return "\n".join(lines)


def post_webhook(url: str, text: str) -> None:
    body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read(256)


def post_app_bot(app_id: str, app_secret: str, chat_id: str, text: str) -> None:
    auth_body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    auth_req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=auth_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(auth_req, timeout=10) as response:
        token = json.loads(response.read().decode("utf-8")).get("tenant_access_token")
    if not token:
        raise RuntimeError("feishu token missing")
    msg = json.dumps({"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text})}).encode("utf-8")
    msg_req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        data=msg,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(msg_req, timeout=10) as response:
        response.read(256)


def deliver(payload: dict[str, Any]) -> str:
    text = format_text(payload)
    webhook = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    chat_id = os.environ.get("FEISHU_CHAT_ID", "").strip()
    if webhook:
        post_webhook(webhook, text)
        return "webhook"
    if app_id and app_secret and chat_id:
        post_app_bot(app_id, app_secret, chat_id, text)
        return "app_bot"
    return "skipped"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] not in {"/", "/healthz"}:
            self.send_error(404)
            return
        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/webhook":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 1_000_000)) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("payload")
            result = deliver(payload)
        except (json.JSONDecodeError, ValueError, urllib.error.URLError, urllib.error.HTTPError, OSError, RuntimeError) as exc:
            sys.stderr.write(f"feishu delivery failed: {type(exc).__name__}\n")
            self.send_response(503)
            self.end_headers()
            return
        body = (result + "\n").encode("utf-8")
        status = 204 if result == "skipped" else 200
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    bind = os.environ.get("LUMA_OBSERVE_BIND", "127.0.0.1")
    port = int(os.environ.get("LUMA_OBSERVE_PORT", "9095"))
    server = ThreadingHTTPServer((bind, port), Handler)
    sys.stderr.write(f"luma-observe feishu-webhook listening on {bind}:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
