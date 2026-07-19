"""Framework-independent primitives shared by LAE components."""

from .runtime import VERSION, component_payload, emit_json, run_component

__all__ = ["VERSION", "component_payload", "emit_json", "run_component"]
