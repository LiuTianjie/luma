"""Normalize bounded SDK lifecycle records and OTLP trace responses."""

import base64
import json
import math
import re
from typing import Mapping, Sequence


EVENT_FIELDS = {
    "event", "timestamp", "instance", "task_name", "task_id", "attempt_id", "runtime", "queue",
    "retry", "trace_id", "span_id", "stage", "progress_at",
    "outcome", "business_outcome", "business_status", "error_type", "elapsed_seconds", "app", "alloc", "node", "task",
    "error_message", "error_location", "error_truncated", "phase_started_at", "phase_finished_at",
    "phase_duration_ms", "phase_count", "phase_outcome", "phase_omitted",
}
TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
EVENT_NAMES = {"task.started": "task_started", "task.finished": "task_finished", "task.stage": "task_stage",
               "task.published": "task_published", "business_outcome": "business_outcome",
               "task.phase.started": "phase_started", "task.phase.finished": "phase_finished",
               "task.failure": "task_failure", "task.phases.omitted": "phases_omitted"}


def normalize_event(record: dict, attribute_aliases: Mapping[str, str] | None = None) -> dict:
    """将 SDK 日志与应用事件整理成受限的执行证据。"""
    aliases = attribute_aliases or {}
    allowed_fields = EVENT_FIELDS | aliases.keys()
    payload = record
    if isinstance(record.get("_msg"), str) and record["_msg"].startswith("{"):
        payload = json.loads(record["_msg"])
    if not isinstance(payload, dict):
        raise ValueError("invalid_event")
    attributes = payload.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {key.removeprefix("attributes."): value for key, value in record.items() if key.startswith("attributes.")}
    event = {key: value for source in (record, payload, attributes) for key, value in source.items() if key in allowed_fields}
    for target, source in (
        ("task_name", "task.name"), ("task_id", "messaging.message.id"), ("runtime", "task.framework"),
        ("queue", "task.queue"), ("retry", "task.retry.attempt"), ("business_outcome", "task.business.outcome"),
        ("business_status", "task.business.status"), ("error_type", "error.type"), ("elapsed_seconds", "duration_seconds"),
        ("stage", "task.stage"),
        ("error_message", "error.message"), ("error_location", "error.location"), ("error_truncated", "error.truncated"),
        ("phase_started_at", "phase.started_at"), ("phase_finished_at", "phase.finished_at"),
        ("phase_duration_ms", "phase.duration_ms"), ("phase_count", "phase.count"),
        ("phase_outcome", "phase.outcome"), ("phase_omitted", "phase.omitted"),
    ):
        if source in attributes:
            event[target] = attributes[source]
    for target, source in aliases.items():
        if source in attributes:
            event[target] = attributes[source]
    if payload.get("service.instance.id"):
        event["instance"] = payload["service.instance.id"]
    event["event"] = EVENT_NAMES.get(event.get("event"), event.get("event", ""))
    if event.get("trace_id") and event.get("span_id"):
        event.setdefault("attempt_id", f'{event["trace_id"]}:{event["span_id"]}')
    return event


MAX_SPANS = 1500
TRACE_ATTRIBUTES = {
    "service.name", "service.namespace", "service.instance.id", "service.version", "deployment.environment.name",
    "luma.stack", "luma.task", "luma.region",
    # Standard SDK, deployment and OpenTelemetry fields.
    "task.name", "task.framework", "task.queue", "task.outcome", "task.retry.attempt", "task.stage",
    "task.business.outcome", "task.business.status", "messaging.message.id",
    "outcome", "error_type",
    "error.type", "exception.type", "http.request.method", "http.response.status_code",
    "error.message", "error.location", "error.truncated", "phase.started_at", "phase.finished_at",
    "phase.duration_ms", "phase.count", "phase.outcome", "phase.omitted",
    "http.method", "http.status_code", "db.system", "db.operation.name", "db.operation",
    "messaging.system", "messaging.operation.name", "messaging.destination.name",
    "rpc.system", "rpc.service", "rpc.method", "gen_ai.system", "gen_ai.request.model",
    "gen_ai.response.model", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
}


def otlp_id(value: str, length: int) -> str:
    """统一 Tempo 十六进制与 OTLP JSON 字节编号。"""
    if re.fullmatch(r"[0-9a-fA-F]{%d}" % length, value):
        return value.lower()
    try:
        decoded = base64.b64decode(value, validate=True).hex()
        return decoded if len(decoded) == length else ""
    except ValueError:
        return ""


def attributes(items: list, allowed: set[str]) -> dict:
    """提取允许展示的标量属性，并限制单个字段长度。"""
    result = {}
    for item in items:
        key = item.get("key")
        if key not in allowed:
            continue
        value = item.get("value", {})
        for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if kind in value:
                result[key] = str(value[kind])[:256]
                break
    return result


def normalize_trace(body: dict, trace_id: str, *, resource_attributes: Mapping[str, str],
                    extra_attributes: Sequence[str] = ()) -> dict:
    """Normalize OTLP within an explicit resource scope and bounded detail budget."""
    allowed = TRACE_ATTRIBUTES | set(resource_attributes) | set(extra_attributes)
    spans = []
    seen = set()
    skipped = 0
    truncated = False
    batches = body.get("batches", body.get("resourceSpans"))
    if not isinstance(batches, list):
        raise ValueError("invalid_response")
    for batch in batches:
        resource = attributes(batch.get("resource", {}).get("attributes", []), allowed)
        scoped = bool(resource_attributes) and all(resource.get(key) == value for key, value in resource_attributes.items())
        for scope in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", [])):
            for span in scope.get("spans", []):
                if not scoped or otlp_id(span.get("traceId", ""), 32) != trace_id:
                    skipped += 1
                    continue
                span_id = otlp_id(span.get("spanId", ""), 16)
                if not span_id or span_id in seen:
                    continue
                start, end = int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"])
                if start <= 0 or end < start:
                    skipped += 1
                    continue
                if len(spans) >= MAX_SPANS:
                    truncated = True
                    continue
                seen.add(span_id)
                attrs = attributes(span.get("attributes", []), allowed)
                events = [{"name": str(event.get("name", ""))[:160],
                           "timeUnixNano": str(int(event.get("timeUnixNano", start))),
                           "attributes": attributes(event.get("attributes", []), allowed)}
                          for event in span.get("events", [])[:32]]
                links = [{"traceId": otlp_id(link.get("traceId", ""), 32),
                          "spanId": otlp_id(link.get("spanId", ""), 16)}
                         for link in span.get("links", [])[:16]]
                code = span.get("status", {}).get("code", 0)
                failed = code in (2, "2", "STATUS_CODE_ERROR") or "failed" in (attrs.get("task.outcome"), attrs.get("task.business.outcome"))
                spans.append({
                    "spanId": span_id, "parentSpanId": otlp_id(span.get("parentSpanId", ""), 16),
                    "name": str(span.get("name", ""))[:200], "service": resource.get("service.name", "unknown"),
                    "kind": str(span.get("kind", "0")), "status": "error" if failed else "ok" if code in (1, "1", "STATUS_CODE_OK") else "unset",
                    "startTimeUnixNano": str(start), "endTimeUnixNano": str(end),
                    "attributes": attrs, "resource": resource, "events": events,
                    "links": [link for link in links if link["traceId"] and link["spanId"]],
                    "droppedAttributes": int(span.get("droppedAttributesCount", 0)),
                    "detailsTruncated": len(span.get("events", [])) > 32 or len(span.get("links", [])) > 16,
                })
    spans.sort(key=lambda span: int(span["startTimeUnixNano"]))
    return {"state": "ok" if spans else "not_found", "traceId": trace_id, "spans": spans,
            "truncated": truncated, "partial": bool(skipped), "limit": MAX_SPANS}


def finite(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None
