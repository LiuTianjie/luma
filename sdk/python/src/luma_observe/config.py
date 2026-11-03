"""Application-owned settings for the shared standard OTLP pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


def _boolean(value: str, default: bool) -> bool:
    return {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}.get(value.strip().lower(), default)


@dataclass(frozen=True)
class Configuration:
    service_name: str | None = None
    enabled: bool | None = None
    resource_attributes: Mapping | None = None
    lifecycle_logs: bool = False
    # "stdout"/"stderr" makes the SDK write lifecycle JSON there itself; None uses the
    # application's own logging configuration for the luma_observe.events logger.
    log_stream: str | None = None
    queue_size: int = 512
    batch_size: int = 64
    export_interval_ms: int = 5000
    metric_interval_ms: int = 30000
    export_timeout_seconds: float = 1.5
    shutdown_timeout_seconds: float = 2.0
    # The application's business outcomes in precedence order, first wins. Only
    # declared values become metric labels; escalation keeps the earliest listed.
    business_outcomes: tuple = ()
    # Opt in to bounded exception text and stacks in the existing lifecycle log queue.
    capture_error_details: bool = False

    def __post_init__(self):
        if isinstance(self.business_outcomes, list):
            object.__setattr__(self, "business_outcomes", tuple(self.business_outcomes))

    def disabled(self) -> bool:
        """Read explicit switches without requiring an endpoint or starting workers."""
        if _boolean(os.getenv("OTEL_SDK_DISABLED", ""), False):
            return True
        if not _boolean(os.getenv("LUMA_OBSERVE_ENABLED", ""), True):
            return True
        return self.enabled is False

    def active(self) -> bool:
        """Require a configured endpoint before creating providers."""
        if self.disabled():
            return False
        return any(os.getenv(key, "").strip() for key in (
            "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        ))

    def signal_active(self, signal: str) -> bool:
        """Enable only explicitly reachable OTLP signals and honor exporter switches."""
        exporter = os.getenv(f"OTEL_{signal.upper()}_EXPORTER", "otlp").strip().lower()
        protocol = os.getenv(f"OTEL_EXPORTER_OTLP_{signal.upper()}_PROTOCOL",
                             os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"))
        return bool(self.active() and exporter == "otlp" and protocol == "http/protobuf" and (
            os.getenv(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        ))

    def validate(self) -> None:
        """Bound the independent process buffers and shutdown budget."""
        if not 1 <= self.batch_size <= self.queue_size <= 8192:
            raise ValueError("require 1 <= batch_size <= queue_size <= 8192")
        if not 100 <= self.export_interval_ms <= 60000 or not 1000 <= self.metric_interval_ms <= 300000:
            raise ValueError("export intervals exceed SDK bounds")
        if not 0 < self.export_timeout_seconds <= 10 or not 0 < self.shutdown_timeout_seconds <= 10:
            raise ValueError("export and shutdown timeouts must be between 0 and 10 seconds")
        if self.log_stream not in (None, "stdout", "stderr"):
            raise ValueError("log_stream must be stdout, stderr or None")
        outcomes = self.business_outcomes
        if (not isinstance(outcomes, tuple) or len(outcomes) > 32 or len(set(outcomes)) != len(outcomes)
                or not all(isinstance(value, str) and 1 <= len(value) <= 64 for value in outcomes)):
            raise ValueError("business_outcomes must list at most 32 distinct values of 1-64 characters")
