"""Read-only observability commands shared with the web investigation views."""
from __future__ import annotations
import json
import re
import time
from datetime import datetime
from urllib.parse import urlencode
from ..errors import LumaError
from . import common


def _timestamp(value: str, now: int, *, duration: bool = False) -> int:
    if not value:
        return now
    match = re.fullmatch(r"(\d+)([smhd])", value) if duration else None
    if match:
        return now-int(match[1])*{"s":1,"m":60,"h":3600,"d":86400}[match[2]]
    try:
        if value.isdigit():
            return int(value)
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("timezone required")
        return int(date.timestamp())
    except ValueError as exc:
        raise LumaError("time must be Unix seconds or RFC3339 with a timezone") from exc


def cmd_observe(args) -> int:
    endpoint, token, insecure, resolve_ip = common._control_context(args, require_token=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    until = _timestamp(getattr(args,"until",""), int(time.time()))
    since = _timestamp(getattr(args,"since","") or "1h", until, duration=True)
    if not 0 < until-since <= 259200:
        raise LumaError("observation range must be at most 72 hours, with start before end")
    command = args.observe_command
    query = {"app": args.name, "since": since, "until": until} if command in {"app", "tasks"} else {}
    for key in ("environment", "service", "instance", "keyword", "trace_id", "task_id", "task_name", "queue", "outcome", "offset"):
        value = getattr(args, key, "")
        if value != "":
            query[key] = value
    if command == "alerts":
        result = {}
        for source, path in (("control", "/v1/alerting/incidents"), ("alertmanager", "/v1/dashboard/observe/alerts")):
            try:
                result[source] = client.request("GET", path + "?" + urlencode({"app":args.name,"limit":50}))
            except LumaError as exc:
                result[source] = {"state": "unreachable", "message": str(exc), "items": []}
    else:
        path = "tasks/" + args.view if command == "tasks" else "status"
        result = client.request("GET", "/v1/dashboard/observe/" + path + "?" + urlencode(query))
    if command in {"app", "tasks"}:
        result["range"] = {"since": since, "until": until}
    common._print_success(args, result) if common._output_format(args) != "text" else print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
