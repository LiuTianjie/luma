"""Optional, business-agnostic OpenTelemetry application integration."""

from .runtime import Configuration, Runtime, TaskRun, configure, current_task, force_flush, get_runtime, shutdown, telemetry_disabled
from .runtime import mark_progress, observed_stage, stage
from .runtime import SafeInstrument

__version__ = "0.4.0"
__all__ = ["Configuration", "Runtime", "TaskRun", "configure", "get_runtime", "shutdown",
           "current_task", "force_flush", "telemetry_disabled", "stage", "observed_stage", "mark_progress",
           "get_tracer", "get_meter", "span", "task", "publication", "counter", "histogram"]


def get_tracer(name="luma.observe"):
    return get_runtime().get_tracer(name)


def get_meter(name="luma.observe"):
    return get_runtime().get_meter(name)


def span(name, **options):
    return get_runtime().span(name, **options)


def task(name, **options):
    return get_runtime().task(name, **options)


def publication(name, **options):
    """Observe an application's own transport send for one task message."""
    return get_runtime().publication(name, **options)


def counter(name, *, unit="1", description=""):
    """A process-aware handle that can be declared before application startup."""
    return SafeInstrument(factory=lambda runtime: runtime.get_meter().create_counter(name, unit=unit, description=description))


def histogram(name, *, unit="s", description=""):
    """A process-aware handle that rebinds when startup or fork creates providers."""
    return SafeInstrument(factory=lambda runtime: runtime.get_meter().create_histogram(name, unit=unit, description=description))
