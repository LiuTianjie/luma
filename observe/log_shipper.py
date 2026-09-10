#!/usr/bin/env python3
"""Ship Nomad allocation stdout/stderr to VictoriaLogs.

Talks to Nomad on the manager loopback. VictoriaLogs down drops the batch;
Nomad, Control and application tasks are not blocked.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

MAX_SOURCES = 64
MAX_LINE = 32768
READ_LIMIT = 65536
FIRST_TAIL = 65536
POLL_INTERVAL = 2.0
INSERT_TIMEOUT = 4.0
INSERT_MAX_BYTES = 1024 * 1024
REQUEST_TIMEOUT = 4.0
CLIENT_STATUSES = {"running", "failed", "complete", "lost"}
STREAMS = ("stdout", "stderr")


@dataclass(frozen=True)
class Source:
    alloc: str
    job: str
    task: str
    task_group: str
    stream: str
    node: str

    @property
    def key(self) -> str:
        return f"{self.alloc}/{self.task}/{self.stream}"

    @property
    def app(self) -> str:
        return self.job


@dataclass
class Cursor:
    file: str
    offset: int
    leftover: bytes = b""


def fetch_json(addr: str, path: str, token: str, timeout: float = REQUEST_TIMEOUT) -> Any:
    url = addr.rstrip("/") + path
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Nomad-Token"] = token
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_bytes(addr: str, path: str, token: str, limit: int, timeout: float = REQUEST_TIMEOUT) -> bytes:
    url = addr.rstrip("/") + path
    headers: dict[str, str] = {}
    if token:
        headers["X-Nomad-Token"] = token
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(limit)


def sources_from_allocations(allocations: Any) -> list[Source]:
    rows: list[Source] = []
    if not isinstance(allocations, list):
        return rows
    for alloc in allocations:
        if not isinstance(alloc, dict):
            continue
        alloc_id = str(alloc.get("ID") or "").strip()
        job = str(alloc.get("JobID") or "").strip()
        status = str(alloc.get("ClientStatus") or "")
        if not alloc_id or not job or status not in CLIENT_STATUSES:
            continue
        tasks = alloc.get("TaskStates") if isinstance(alloc.get("TaskStates"), dict) else {}
        names = list(tasks) or [str(alloc.get("TaskGroup") or job)]
        group = str(alloc.get("TaskGroup") or "")
        node = str(alloc.get("NodeName") or "")
        for task in names:
            for stream in STREAMS:
                rows.append(Source(alloc=alloc_id, job=job, task=str(task), task_group=group, stream=stream, node=node))
    return rows


def round_robin(sources: list[Source], start: int, cap: int = MAX_SOURCES) -> list[Source]:
    if not sources:
        return []
    index = start % len(sources)
    ordered = sources[index:] + sources[:index]
    return ordered[:cap]


def log_files(entries: Any, task: str, stream: str) -> list[tuple[int, str, int]]:
    prefix = f"{task}.{stream}."
    rows: list[tuple[int, str, int]] = []
    if not isinstance(entries, list):
        return rows
    for item in entries:
        if not isinstance(item, dict) or item.get("IsDir"):
            continue
        name = str(item.get("Name") or "")
        if not name.startswith(prefix) or not name[len(prefix) :].isdigit():
            continue
        rows.append((int(name[len(prefix) :]), "alloc/logs/" + name, max(int(item.get("Size") or 0), 0)))
    return sorted(rows)


def start_cursor(files: list[tuple[int, str, int]], tail: int = FIRST_TAIL) -> Cursor | None:
    if not files:
        return None
    _index, path, size = files[-1]
    offset = max(0, size - tail)
    return Cursor(file=path, offset=offset)


def split_lines(data: bytes, leftover: bytes, max_line: int = MAX_LINE) -> tuple[list[str], bytes]:
    buf = leftover + data
    lines: list[str] = []
    start = 0
    while True:
        newline = buf.find(b"\n", start)
        if newline < 0:
            break
        chunk = buf[start:newline].rstrip(b"\r")
        if len(chunk) > max_line:
            chunk = chunk[:max_line]
        lines.append(chunk.decode("utf-8", errors="replace"))
        start = newline + 1
    rest = buf[start:]
    if len(rest) > max_line:
        lines.append(rest[:max_line].decode("utf-8", errors="replace"))
        rest = b""
    return lines, rest


def rfc3339(now: float | None = None) -> str:
    stamp = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def records_for(source: Source, lines: list[str], observed_at: str) -> list[dict[str, str]]:
    return [
        {
            "_time": observed_at,
            "_msg": line,
            "app": source.app,
            "job": source.job,
            "task": source.task,
            "task_group": source.task_group,
            "alloc": source.alloc,
            "stream": source.stream,
            "node": source.node,
        }
        for line in lines
    ]


def insert_jsonline_url(base: str) -> str:
    return (
        base.rstrip("/")
        + "/insert/jsonline?_msg_field=_msg&_time_field=_time&_stream_fields=app,job,task,stream"
    )


def encode_jsonline(rows: list[dict[str, str]]) -> bytes:
    return ("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)).encode("utf-8")


def post_jsonline(url: str, rows: list[dict[str, str]], timeout: float = INSERT_TIMEOUT) -> None:
    payload = encode_jsonline(rows)
    if not payload:
        return
    offset = 0
    while offset < len(payload):
        chunk = payload[offset : offset + INSERT_MAX_BYTES]
        newline = chunk.rfind(b"\n")
        if newline < 0 and offset + INSERT_MAX_BYTES < len(payload):
            raise OSError("log line exceeds VictoriaLogs insert batch")
        if newline >= 0 and offset + INSERT_MAX_BYTES < len(payload):
            chunk = chunk[: newline + 1]
        request = urllib.request.Request(url, data=chunk, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
        offset += len(chunk)


class Shipper:
    def __init__(
        self,
        *,
        list_allocs: Callable[[], Any],
        list_files: Callable[[str], Any],
        readat: Callable[[str, str, int, int], bytes],
        insert: Callable[[list[dict[str, str]]], None],
    ) -> None:
        self.list_allocs = list_allocs
        self.list_files = list_files
        self.readat = readat
        self.insert = insert
        self.cursors: dict[str, Cursor] = {}
        self.round = 0
        self.exported = 0
        self.dropped = 0
        self.nomad_up = 1
        self.last_sources = 0

    def poll(self) -> None:
        try:
            allocations = self.list_allocs()
            self.nomad_up = 1
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, ValueError):
            self.nomad_up = 0
            return
        sources = sources_from_allocations(allocations)
        self.last_sources = len(sources)
        batch: list[dict[str, str]] = []
        visited = round_robin(sources, self.round)
        live = {source.key for source in sources}
        observed = rfc3339()
        for source in visited:
            try:
                files = log_files(self.list_files(source.alloc), source.task, source.stream)
            except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, ValueError):
                continue
            cursor = self.cursors.get(source.key)
            if cursor is None:
                cursor = start_cursor(files)
                if cursor is None:
                    continue
            matching = [item for item in files if item[1] == cursor.file]
            if not matching:
                prefix = f"{source.task}.{source.stream}."
                prior = -1
                name = cursor.file.rsplit("/", 1)[-1]
                if name.startswith(prefix) and name[len(prefix) :].isdigit():
                    prior = int(name[len(prefix) :])
                files = [item for item in files if item[0] > prior]
                if not files:
                    continue
                cursor = Cursor(file=files[0][1], offset=0)
            else:
                files = [item for item in files if item[0] >= matching[0][0]]
            leftover = cursor.leftover
            for _index, path, size in files:
                start = cursor.offset if path == cursor.file else 0
                if size < start:
                    start = 0
                    leftover = b""
                if size <= start:
                    cursor = Cursor(file=path, offset=start, leftover=leftover)
                    continue
                try:
                    data = self.readat(source.alloc, path, start, min(size - start, READ_LIMIT))
                except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
                    break
                lines, leftover = split_lines(data, leftover)
                cursor = Cursor(file=path, offset=start + len(data), leftover=leftover)
                batch.extend(records_for(source, lines, observed))
                if len(data) >= READ_LIMIT:
                    break
            self.cursors[source.key] = cursor
        self.round += len(visited)
        self.cursors = {key: value for key, value in self.cursors.items() if key in live}
        if not batch:
            return
        try:
            self.insert(batch)
            self.exported += len(batch)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            self.dropped += len(batch)

    def metrics(self) -> str:
        return "".join(
            [
                "# HELP luma_observe_log_shipper_up 1 if the shipper process is running.\n",
                "# TYPE luma_observe_log_shipper_up gauge\n",
                "luma_observe_log_shipper_up 1\n",
                "# HELP luma_observe_log_shipper_sources Allocation log sources discovered this poll.\n",
                "# TYPE luma_observe_log_shipper_sources gauge\n",
                f"luma_observe_log_shipper_sources {self.last_sources}\n",
                "# HELP luma_observe_log_shipper_exported_lines_total Lines accepted by VictoriaLogs.\n",
                "# TYPE luma_observe_log_shipper_exported_lines_total counter\n",
                f"luma_observe_log_shipper_exported_lines_total {self.exported}\n",
                "# HELP luma_observe_log_shipper_dropped_lines_total Lines dropped because VictoriaLogs was unavailable.\n",
                "# TYPE luma_observe_log_shipper_dropped_lines_total counter\n",
                f"luma_observe_log_shipper_dropped_lines_total {self.dropped}\n",
                "# HELP luma_observe_log_shipper_nomad_up 1 if the last Nomad list succeeded.\n",
                "# TYPE luma_observe_log_shipper_nomad_up gauge\n",
                f"luma_observe_log_shipper_nomad_up {self.nomad_up}\n",
            ]
        )


def nomad_list_allocs(addr: str, token: str) -> Any:
    return fetch_json(addr, "/v1/allocations?resources=false", token)


def nomad_list_files(addr: str, token: str, alloc: str) -> Any:
    query = urllib.parse.urlencode({"path": "alloc/logs"})
    return fetch_json(addr, f"/v1/client/fs/ls/{urllib.parse.quote(alloc, safe='')}?{query}", token)


def nomad_readat(addr: str, token: str, alloc: str, path: str, offset: int, limit: int) -> bytes:
    query = urllib.parse.urlencode({"path": path, "offset": offset, "limit": limit})
    return fetch_bytes(addr, f"/v1/client/fs/readat/{urllib.parse.quote(alloc, safe='')}?{query}", token, limit)


class Handler(BaseHTTPRequestHandler):
    shipper: Shipper

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] not in {"/", "/metrics", "/healthz"}:
            self.send_error(404)
            return
        body = self.shipper.metrics().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    bind = os.environ.get("LUMA_OBSERVE_BIND", "127.0.0.1")
    port = int(os.environ.get("LUMA_OBSERVE_PORT", "9108"))
    addr = os.environ.get("NOMAD_ADDR", "http://127.0.0.1:4646")
    token = os.environ.get("NOMAD_TOKEN", "").strip()
    logs_url = insert_jsonline_url(os.environ.get("VICTORIALOGS_URL", "http://127.0.0.1:9428"))
    shipper = Shipper(
        list_allocs=lambda: nomad_list_allocs(addr, token),
        list_files=lambda alloc: nomad_list_files(addr, token, alloc),
        readat=lambda alloc, path, offset, limit: nomad_readat(addr, token, alloc, path, offset, limit),
        insert=lambda rows: post_jsonline(logs_url, rows),
    )
    Handler.shipper = shipper
    server = ThreadingHTTPServer((bind, port), Handler)
    server.timeout = POLL_INTERVAL
    sys.stderr.write(f"luma-observe log-shipper listening on {bind}:{port}\n")
    last_stats = 0.0
    try:
        while True:
            shipper.poll()
            now = time.monotonic()
            if now - last_stats >= 60:
                sys.stderr.write(
                    f"log-shipper sources={shipper.last_sources} exported={shipper.exported} dropped={shipper.dropped}\n"
                )
                last_stats = now
            server.handle_request()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
