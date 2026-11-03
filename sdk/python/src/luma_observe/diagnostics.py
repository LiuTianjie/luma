"""Bounded per-attempt stage summaries and explicitly enabled exception details."""

import threading
import time
import traceback


MAX_PHASES = 8
ERROR_MESSAGE_BYTES = 4096
ERROR_STACK_BYTES = 16384


def clip_utf8(text: str, limit: int) -> str:
    return text.encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="ignore")


def error_attributes(error: BaseException, details: bool) -> dict:
    fields = {"error.type": type(error).__name__}
    if details:
        text = str(error)
        fields.update({"error.message": text[:256], "error.truncated": len(text) > 256})
        frame = error.__traceback__
        for _ in range(64):
            if frame is None:
                break
            fields["error.location"] = f"{frame.tb_frame.f_code.co_filename}:{frame.tb_lineno}"[-256:]
            frame = frame.tb_next
    return fields


def exception_details(error: BaseException) -> dict:
    """Keep messages and cause chains in lifecycle logs, with explicit UTF-8 budgets."""
    message = str(error)
    stack = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    message_text = clip_utf8(message, ERROR_MESSAGE_BYTES)
    stack_text = clip_utf8(stack, ERROR_STACK_BYTES)
    return {"error_type": type(error).__name__, "error_message": message_text,
            "stacktrace": stack_text, "truncated": message_text != message or stack_text != stack}


class StageSummaries:
    """Aggregate fixed stage names across a batch; retain only bounded scalar data."""

    def __init__(self):
        self.rows = {}
        self.failure = None
        self.omitted = 0
        self.lock = threading.Lock()

    def start(self, name: str, attributes: dict):
        with self.lock:
            first = None
            if name not in self.rows:
                if len(self.rows) >= MAX_PHASES:
                    self.omitted += 1
                    return None, None
                self.rows[name] = {"task.stage": name, "phase.started_at": time.time(),
                                   "phase.duration_ms": 0.0, "phase.count": 0,
                                   "phase.outcome": "succeeded"}
                first = {**self.rows[name], **attributes}
            return time.monotonic(), first

    def finish(self, name: str, started: float | None, attributes: dict, error: dict):
        with self.lock:
            if started is None:
                return
            row = self.rows[name]
            row["phase.duration_ms"] += max(0, time.monotonic() - started) * 1000
            row["phase.count"] += 1
            row["phase.finished_at"] = time.time()
            if row["phase.outcome"] != "failed":
                row.update(attributes)
                if error:
                    row.update({"phase.outcome": "failed", **error})

    def claim_failure(self, attributes: dict) -> bool:
        with self.lock:
            if self.failure is not None:
                return False
            self.failure = attributes
            return True

    def completed(self):
        with self.lock:
            return [dict(row) for row in self.rows.values()], self.omitted
