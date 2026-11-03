#!/usr/bin/env python3
"""Compact an authenticated CLI log page; retain exact identities and evidence gaps."""
from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import urlencode, urlsplit


TRACE_ID = re.compile(r"[0-9a-f]{32}")
ATTRIBUTE_FIELDS = {
    "taskName": "task.name", "taskId": "messaging.message.id",
    "framework": "task.framework", "retry": "task.retry.attempt",
    "stage": "task.stage", "outcome": "outcome",
    "businessOutcome": "task.business.outcome", "errorType": "error.type",
    "errorMessage": "error.message", "errorLocation": "error.location",
    "durationSeconds": "duration_seconds", "phaseCount": "phase.count",
    "phaseDurationMs": "phase.duration_ms", "phaseOutcome": "phase.outcome",
}


def trace_url(origin: str, trace_id: str) -> str:
    panes = {"task": {"datasource": "tempo", "queries": [
        {"refId": "A", "queryType": "traceql", "query": trace_id}
    ], "range": {"from": "now-24h", "to": "now"}}}
    return origin + "/grafana/explore?" + urlencode({
        "schemaVersion": 1, "panes": json.dumps(panes, separators=(",", ":")),
    })


def summarize(payload: dict, *, origin: str = "", selected_trace: str = "", full_lines: bool = False) -> dict:
    if payload.get("ok") is not True:
        return {"ok": False, "error": payload.get("error", "CLI query failed"), "command": payload.get("command")}
    result = payload["result"]
    entries, traces = [], {}
    for source in result["entries"]:
        line = source["line"]
        try:
            record = json.loads(line)
        except ValueError:
            record = {}
        if not isinstance(record, dict):
            record = {}
        attributes = record.get("attributes", {})
        if not isinstance(attributes, dict):
            attributes = {}
        trace_id = record.get("trace_id", "")
        trace_id = trace_id if isinstance(trace_id, str) and TRACE_ID.fullmatch(trace_id) and int(trace_id, 16) else ""
        row = {key: source[key] for key in ("observedAt", "task", "allocationId", "node", "stream", "truncated") if key in source}
        row.update({key: record[key] for key in ("event", "timestamp", "span_id") if key in record})
        row.update({key: attributes[field] for key, field in ATTRIBUTE_FIELDS.items() if field in attributes})
        if trace_id:
            row["traceId"] = trace_id
            traces[trace_id] = traces.get(trace_id, 0) + 1
        if selected_trace:
            row["association"] = ("exact" if trace_id == selected_trace else "other_trace" if trace_id
                                  else "mention" if selected_trace in line else "unrelated")
        row["line"] = line if full_lines else line[:600]
        row["previewTruncated"] = len(row["line"]) < len(line)
        entries.append(row)
    links = [{"traceId": trace_id, "recordsOnPage": count,
              **({"url": trace_url(origin, trace_id)} if origin else {})} for trace_id, count in traces.items()]
    return {"ok": True, **{key: result[key] for key in ("app", "range", "page", "warnings") if key in result},
            "entriesOnPage": len(entries), "traces": links, "entries": entries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-url", default="", help="Control origin from 'luma context list'; used only to build links")
    parser.add_argument("--trace-id", default="", help="Mark exact structured matches versus text mentions")
    parser.add_argument("--full-lines", action="store_true", help="Keep full messages returned by the CLI")
    args = parser.parse_args()
    origin = args.control_url.rstrip("/")
    if origin:
        try:
            parsed = urlsplit(origin)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None
                     and parsed.password is None and not (parsed.path or parsed.query or parsed.fragment)
                     and not any(char.isspace() for char in origin))
            parsed.port  # Validate the port when present.
        except ValueError:
            valid = False
        if not valid:
            parser.error("--control-url must be an HTTP(S) origin without credentials, path or query")
    if args.trace_id and (not TRACE_ID.fullmatch(args.trace_id) or not int(args.trace_id, 16)):
        parser.error("--trace-id must be a nonzero 32-character lowercase hexadecimal ID")
    try:
        output = summarize(json.load(sys.stdin), origin=origin, selected_trace=args.trace_id, full_lines=args.full_lines)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        print(json.dumps({"ok": False, "error": "Expected one 'luma app log-query --format json' response",
                          "detail": type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
