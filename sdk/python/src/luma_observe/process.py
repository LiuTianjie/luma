"""Process ownership around official OpenTelemetry exporters.

OTel restarts its batch and metric reader threads after a fork. An inherited
reader still owns its parent's resource and metric state, so those threads must
not export while a worker creates its own providers.
"""

from __future__ import annotations

import os
import threading
import weakref
from collections.abc import Sequence

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, MetricsData
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


class ProcessSpanExporter(SpanExporter):
    """Delegate to an official exporter only in the process that created it."""

    def __init__(self, exporter: SpanExporter):
        self._exporter = exporter
        self._pid = os.getpid()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if os.getpid() != self._pid:
            return SpanExportResult.FAILURE
        return self._exporter.export(spans)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        if os.getpid() != self._pid:
            return False
        return self._exporter.force_flush(timeout_millis=timeout_millis)

    def shutdown(self) -> None:
        # Closing a copied transport is safe in the child and leaves the
        # parent's file descriptors and transport untouched.
        self._exporter.shutdown()


class ProcessMetricExporter(MetricExporter):
    """Preserve official aggregation preferences while fencing inherited data."""

    def __init__(self, exporter: MetricExporter):
        # These are the preference fields defined by MetricExporter and read
        # by PeriodicExportingMetricReader; OTel provides no public getters.
        super().__init__(
            preferred_temporality=exporter._preferred_temporality,
            preferred_aggregation=exporter._preferred_aggregation,
        )
        self._exporter = exporter
        self._pid = os.getpid()

    def export(self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs) -> MetricExportResult:
        if os.getpid() != self._pid:
            return MetricExportResult.FAILURE
        return self._exporter.export(metrics_data, timeout_millis=timeout_millis, **kwargs)

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        if os.getpid() != self._pid:
            return False
        return self._exporter.force_flush(timeout_millis=timeout_millis)

    def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
        self._exporter.shutdown(timeout_millis=timeout_millis, **kwargs)


def _shutdown_provider(provider: TracerProvider | MeterProvider) -> None:
    try:
        if isinstance(provider, MeterProvider):
            provider.shutdown(timeout_millis=1_000)
        else:
            provider.shutdown()
    except Exception:
        # Cleanup must not propagate into the task process or its log handlers.
        # The process fence remains active if an inherited lock cannot close.
        pass


def register_provider_cleanup(*providers: TracerProvider | MeterProvider) -> None:
    """Close inherited providers without waiting on the child/business thread.

    Call after constructing all providers and readers, so OTel's own fork
    callbacks reinitialize their thread state before this cleanup is scheduled.
    Only weak references survive between forks. The before-fork callback keeps
    providers alive across an earlier child callback clearing a runtime owner.
    """
    if not hasattr(os, "register_at_fork") or not providers:
        return

    references = tuple(weakref.ref(provider) for provider in providers)
    inherited: tuple[TracerProvider | MeterProvider, ...] = ()

    def before_fork() -> None:
        nonlocal inherited
        inherited = tuple(provider for reference in references if (provider := reference()) is not None)

    def after_parent() -> None:
        nonlocal inherited
        inherited = ()

    def after_child() -> None:
        nonlocal inherited
        captured, inherited = inherited, ()
        for provider in captured:
            # Each provider closes independently: a stalled metrics callback
            # cannot keep the span processor alive. No join runs in a fork
            # callback, including for trace providers with no timeout API.
            try:
                threading.Thread(
                    target=_shutdown_provider,
                    args=(provider,),
                    name="luma-observe-fork-cleanup",
                    daemon=True,
                ).start()
            except Exception:
                pass

    os.register_at_fork(before=before_fork, after_in_parent=after_parent, after_in_child=after_child)
