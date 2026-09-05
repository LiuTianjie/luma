"""Bounded management log snapshots and resumable file-offset following.

Nomad does not timestamp its log frames. observedAt is observation time only.
The cursor is untrusted input: it never selects a job, allocation or path. All
reads are derived from the authenticated service's current allocation list.
"""
from __future__ import annotations

import base64
import codecs
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..errors import LumaError

MAX_SOURCES = 32
MAX_BYTES = 65536
POLL_INTERVAL = 2.0
REQUEST_TIMEOUT = 3.0
POLL_BUDGET = 12.0
CAPABILITIES = {"since": False, "resume": True, "allocation": True, "previous": True}
LIMITS = {"maxSources": MAX_SOURCES, "maxBytesPerSource": MAX_BYTES, "pollIntervalSeconds": POLL_INTERVAL, "tailScope": "sharedAcrossSources", "maxLinesPerPoll": 1000}


class LogUnavailable(LumaError):
    """Temporary upstream source discovery failure (HTTP 503)."""


def encode_event(event: dict[str, Any]) -> bytes:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


class LogReader:
    def __init__(self, client: Any, job: str, task: str, service: str, *, tail: int = 120,
                 allocation: str = "", previous: bool = False, cursor: str = ""):
        self.client = client
        self.client.READ_TIMEOUT = REQUEST_TIMEOUT
        self.job, self.task, self.service = job, task, service
        self.tail = min(max(int(tail), 1), 1000)
        self.allocation, self.previous = allocation, previous
        self.positions: dict[str, list[Any]] = {}
        self.sources: list[dict[str, str]] = []
        self._read_sources: list[dict[str, str]] = []
        self._scope = [service, allocation, previous]
        self._warnings: set[str] = set()
        self._round = 0
        self._line_limit = self.tail
        self._follow_limit = 1000
        self.backlog = False
        self._files_cache: dict[str, list[Any]] = {}
        if cursor:
            try:
                if len(cursor) > 24000:
                    raise ValueError()
                value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
                if not isinstance(value, dict) or value.get("v") != 1 or value.get("scope") != self._scope:
                    raise ValueError()
                positions = value["positions"]
                if not isinstance(positions, dict) or len(positions) > MAX_SOURCES:
                    raise ValueError()
                for key, position in positions.items():
                    if not isinstance(key, str) or len(key) > 512 or not isinstance(position, list) or len(position) != 3:
                        raise ValueError()
                    file, offset, partial = position
                    if not isinstance(file, str) or len(file) > 512 or type(offset) is not int or not 0 <= offset <= 2**63 - 1 or type(partial) is not bool:
                        raise ValueError()
                self.positions = positions
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                raise LumaError("invalid logs cursor; refresh the log snapshot") from exc

    def cursor(self) -> str:
        raw = json.dumps({"v": 1, "scope": self._scope, "positions": self.positions}, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode()

    def discover(self) -> list[str]:
        try:
            allocations = self.client.request("GET", f"/v1/job/{urllib.parse.quote(self.job, safe='')}/allocations")
        except LumaError as exc:
            if str(exc).startswith("Nomad API error 404:"):
                raise LumaError("service has no retained Nomad allocations") from exc
            raise LogUnavailable("Nomad log sources are temporarily unavailable") from exc
        if not isinstance(allocations, list):
            raise LogUnavailable("Nomad returned an invalid allocation list")
        all_allocs = [a for a in allocations if isinstance(a, dict) and a.get("ID")]
        active = lambda a: a.get("DesiredStatus") == "run" and a.get("ClientStatus") == "running"
        if self.allocation:
            selected = [a for a in all_allocs if str(a["ID"]) == self.allocation]
            if not selected:
                raise LumaError("allocation does not belong to this service or is no longer retained")
        elif self.previous:
            selected = [a for a in all_allocs if a.get("DesiredStatus") == "stop" or a.get("ClientStatus") in {"complete", "failed", "lost"}]
        else:
            selected = [a for a in all_allocs if active(a)] or all_allocs
        selected.sort(key=lambda a: int(a.get("CreateTime") or a.get("CreateIndex") or 0), reverse=True)
        sources = []
        for allocation in selected:
            tasks = allocation.get("TaskStates") or {}
            names = [self.task] if self.task else list(tasks) or [str(allocation.get("TaskGroup") or self.job)]
            for task in names:
                if tasks and task not in tasks:
                    continue
                for stream in ("stdout", "stderr"):
                    sources.append({"allocationId": str(allocation["ID"]), "task": task, "stream": stream})
        warnings = []
        if len(sources) > MAX_SOURCES:
            warnings.append(f"Reading {MAX_SOURCES} of {len(sources)} log sources; select an allocation to inspect others.")
        self.sources = sources
        self._read_sources = sources[:MAX_SOURCES]
        self._line_limit = max(1, self.tail // max(1, len(self._read_sources)))
        self._follow_limit = max(1, 1000 // max(1, len(self._read_sources)))
        # Bound resume tokens and memory as rolling deployments replace sources.
        live = {self.key(source) for source in self._read_sources}
        self.positions = {k: v for k, v in self.positions.items() if k in live}
        return warnings

    @staticmethod
    def key(source: dict[str, str]) -> str:
        return json.dumps([source["allocationId"], source["task"], source["stream"]], separators=(",", ":"))

    def warning(self, message: str) -> list[dict[str, Any]]:
        if message in self._warnings:
            return []
        if len(self._warnings) >= 128:
            self._warnings.clear()
        self._warnings.add(message)
        return [{"status": "warning", "message": message}]

    def _lines(self, source: dict[str, str], file: str, offset: int, data: bytes, *, initial: bool = False, line_limit: int | None = None, final: bool = False) -> list[dict[str, Any]]:
        key = self.key(source)
        old = self.positions.get(key)
        continued = bool(old and old[0] == file and old[2])
        # Do not split a UTF-8 codepoint at a byte-limited read boundary.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        decoder.decode(data, final=False)
        pending = decoder.getstate()[0]
        if pending and not final:
            data = data[:-len(pending)]
        chunks = data.splitlines(keepends=True)
        if initial and len(chunks) > self._line_limit:
            skipped = chunks[:-self._line_limit]
            offset += sum(map(len, skipped))
            chunks = chunks[-self._line_limit:]
        elif not initial:
            chunks = chunks[:line_limit if line_limit is not None else self._follow_limit]
        events = []
        for chunk in chunks:
            offset += len(chunk)
            partial = not chunk.endswith((b"\n", b"\r"))
            self.positions[key] = [file, offset, partial]
            events.append({"line": chunk.rstrip(b"\r\n").decode("utf-8", errors="replace"), **source,
                           "file": file, "offset": offset, "observedAt": int(time.time()),
                           "partial": partial, "continued": continued, "cursor": self.cursor()})
            continued = partial
        if not chunks:
            self.positions[key] = [file, offset, continued]
        return events

    def _files(self, source: dict[str, str]) -> list[tuple[int, str, int]]:
        query = urllib.parse.urlencode({"path": "alloc/logs"})
        allocation = source["allocationId"]
        if allocation not in self._files_cache:
            files = self.client.request("GET", f"/v1/client/fs/ls/{urllib.parse.quote(allocation, safe='')}?{query}")
            if not isinstance(files, list):
                raise LumaError("Nomad returned an invalid log file list")
            self._files_cache[allocation] = files
        files = self._files_cache[allocation]
        prefix = f"{source['task']}.{source['stream']}."
        candidates = []
        for item in files:
            name = str(item.get("Name") or "") if isinstance(item, dict) else ""
            if name.startswith(prefix) and name[len(prefix):].isdigit() and not item.get("IsDir"):
                candidates.append((int(name[len(prefix):]), "alloc/logs/" + name, max(int(item.get("Size") or 0), 0)))
        return sorted(candidates)

    def _initial(self, source: dict[str, str]) -> list[dict[str, Any]]:
        # Nomad 1.9.7 fs/logs frame Offset is affected by framer coalescing;
        # it is not a reliable start offset. Use stat sizes and bounded raw
        # reads for both snapshot and follow, with exact byte positions.
        files = self._files(source)
        budget = MAX_BYTES
        selected = []
        for _index, file, size in reversed(files):
            length = min(size, budget)
            selected.append((file, size - length, length))
            budget -= length
            if budget <= 0:
                self.backlog = True
                break
        events = []
        deadline = time.monotonic() + REQUEST_TIMEOUT
        for file, start, length in reversed(selected):
            if time.monotonic() >= deadline:
                self.backlog = True
                break
            data = self._read_bytes(source, file, start, length) if length else b""
            final = bool(files and file != files[-1][1] and len(data) >= length)
            events.extend(self._lines(source, file, start, data, initial=True, final=final))
        return events[-self._line_limit:]

    def _read_bytes(self, source: dict[str, str], file: str, offset: int, limit: int) -> bytes:
        query = urllib.parse.urlencode({"path": file, "offset": offset, "limit": limit})
        path = f"/v1/client/fs/readat/{urllib.parse.quote(source['allocationId'], safe='')}?{query}"
        headers = {"X-Nomad-Token": self.client.token} if self.client.token else {}
        request = urllib.request.Request(self.client.api_url + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return response.read(limit)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise LumaError(f"Nomad log file is temporarily unavailable (HTTP {code})") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise LumaError("Nomad log file is temporarily unavailable") from exc

    def _follow(self, source: dict[str, str]) -> list[dict[str, Any]]:
        deadline = time.monotonic() + REQUEST_TIMEOUT
        key = self.key(source)
        file, offset, _partial = self.positions[key]
        # The cursor never controls a read path: match against names returned
        # by Nomad and the authenticated service's task name.
        candidates = self._files(source)
        prefix = f"{source['task']}.{source['stream']}."
        latest_file = candidates[-1][1] if candidates else ""
        events = []
        matching = [item for item in candidates if item[1] == file]
        if not matching:
            events.extend(self.warning(f"Log retention gap for {source['allocationId']}/{source['task']}/{source['stream']}; retained logs resume below."))
            # Skip older files than the cursor when the exact file was GC'd.
            match = re.fullmatch(re.escape("alloc/logs/" + prefix) + r"(\d+)", file)
            prior_index = int(match.group(1)) if match else -1
            candidates = [item for item in candidates if item[0] > prior_index]
        else:
            candidates = [item for item in candidates if item[0] >= matching[0][0]]
        budget = MAX_BYTES
        remaining_lines = self._follow_limit
        for _index, path, size in candidates:
            if time.monotonic() >= deadline or remaining_lines <= 0:
                self.backlog = True
                break
            start = offset if path == file else 0
            if size < start:
                events.extend(self.warning(f"Log file truncated for {source['allocationId']}/{source['task']}/{source['stream']}; reading from start."))
                start = 0
                self.positions[key] = [path, 0, False]
            if size > start and budget:
                data = self._read_bytes(source, path, start, min(size - start, budget))
                new_events = self._lines(source, path, start, data, line_limit=remaining_lines, final=path != latest_file and start + len(data) >= size)
                events.extend(new_events)
                remaining_lines -= len(new_events)
                budget -= len(data)
                if self.positions[key][1] < size:
                    self.backlog = self.backlog or bool(new_events)  # incomplete UTF-8 alone waits for more bytes
                    break
            elif size == start:
                self.positions[key] = [path, start, bool(self.positions[key][2]) if path == file else False]
            if budget <= 0:
                self.backlog = True
                break
        return events

    def poll(self) -> list[dict[str, Any]]:
        self._files_cache.clear()
        self.backlog = False
        events = []
        for message in self.discover():
            events.extend(self.warning(message))
        deadline = time.monotonic() + POLL_BUDGET
        sources = self._read_sources
        if sources:
            # Round robin prevents one slow allocation starving later sources.
            start = self._round % len(sources)
            sources = sources[start:] + sources[:start]
        processed = 0
        for source in sources:
            if time.monotonic() >= deadline:
                events.extend(self.warning("Some log sources are slow; remaining sources will be read on the next poll."))
                break
            try:
                position = self.positions.get(self.key(source))
                if position and not position[0]:
                    events.extend(self.warning("This source has no Nomad file cursor; refresh to retrieve another snapshot."))
                else:
                    events.extend(self._follow(source) if position else self._initial(source))
            except LumaError as exc:
                events.extend(self.warning(f"{source['allocationId']}/{source['task']}/{source['stream']}: {exc}"))
            processed += 1
        self._round += processed
        events.append({"status": "heartbeat", "cursor": self.cursor(), "sources": self.sources, "backlog": self.backlog})
        return events
