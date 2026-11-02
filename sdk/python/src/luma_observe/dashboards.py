"""Build standard Grafana dashboards from application-owned JSON configuration."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping


MAX_BYTES = 1024 * 1024
UID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
INTERVAL = re.compile(r"^(?:[1-9][0-9]*[smhd]|)$")


class DashboardConfigError(ValueError):
    """Application dashboard configuration could not be rendered."""


def _text(value: Any, name: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DashboardConfigError(f"{name} must be a nonempty string of at most {maximum} characters")
    return value


def _object(value: Any, name: str) -> dict:
    if not isinstance(value, Mapping):
        raise DashboardConfigError(f"{name} must be an object")
    return dict(value)


def _description(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise DashboardConfigError(f"{name} must be a string of at most 4096 characters")
    return value


def _list(value: Any, name: str, maximum: int) -> list:
    if not isinstance(value, list) or len(value) > maximum:
        raise DashboardConfigError(f"{name} must be a list of at most {maximum} items")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise DashboardConfigError(f"{name} must be a boolean")
    return value


def _keys(value: Mapping, allowed: set[str], name: str) -> None:
    unexpected = set(value) - allowed
    if unexpected:
        raise DashboardConfigError(f"{name} has unknown fields: {', '.join(sorted(unexpected))}")


def _datasource(uid: str) -> dict:
    return {"type": "prometheus", "uid": _text(uid, "datasource", 160)}


def _variable(config: Any, datasource: dict) -> dict:
    value = _object(config, "variable")
    _keys(value, {"name", "label", "type", "query", "multi", "includeAll", "allValue"}, "variable")
    name = _text(value.get("name"), "variable.name", 64)
    if not NAME.fullmatch(name):
        raise DashboardConfigError("variable.name must contain letters, digits or underscores and start with a letter or underscore")
    kind = value.get("type", "query")
    if kind not in {"query", "custom", "constant"}:
        raise DashboardConfigError("variable.type must be query, custom or constant")
    query = _text(value.get("query"), "variable.query", 16384)
    result = {
        "name": name, "label": _text(value.get("label", name), "variable.label", 160),
        "type": kind, "query": query,
        "multi": _boolean(value.get("multi", False), "variable.multi"),
        "includeAll": _boolean(value.get("includeAll", False), "variable.includeAll"),
        "hide": 2 if kind == "constant" else 0,
    }
    if kind == "query":
        result.update(datasource=deepcopy(datasource), definition=query, refresh=2, sort=1)
        result["query"] = {"query": query, "refId": f"variable-{name}"}
    if result["includeAll"]:
        result["allValue"] = _text(value.get("allValue", ".*"), "variable.allValue", 256)
        result["current"] = {"selected": True, "text": "All", "value": "$__all"}
    elif "allValue" in value:
        raise DashboardConfigError("variable.allValue requires includeAll=true")
    if kind == "constant" and (result["multi"] or result["includeAll"]):
        raise DashboardConfigError("constant variables cannot use multi or includeAll")
    if kind == "constant":
        result["current"] = {"selected": True, "text": query, "value": query}
    return result


def _target(config: Any, index: int, kind: str, datasource: dict) -> dict:
    value = _object(config, "panel target")
    _keys(value, {"expr", "legend", "instant"}, "panel target")
    instant = _boolean(value.get("instant", kind != "timeseries"), "target.instant")
    legend = value.get("legend", "")
    if not isinstance(legend, str) or len(legend) > 256:
        raise DashboardConfigError("target.legend must be a string of at most 256 characters")
    return {
        "refId": chr(ord("A") + index), "datasource": deepcopy(datasource),
        "expr": _text(value.get("expr"), "target.expr", 16384),
        "legendFormat": legend, "instant": instant, "range": not instant,
        "format": "table" if kind == "table" else "time_series",
    }


def _panel(config: Any, index: int, datasource: dict) -> dict:
    value = _object(config, "panel")
    _keys(value, {"title", "description", "type", "expr", "legend", "instant", "targets",
                  "unit", "decimals", "min", "max", "thresholds", "fieldConfig", "options", "transformations"}, "panel")
    kind = value.get("type", "timeseries")
    if kind not in {"stat", "timeseries", "table", "gauge", "bargauge"}:
        raise DashboardConfigError("panel.type must be stat, timeseries, table, gauge or bargauge")
    if "targets" in value:
        if any(key in value for key in ("expr", "legend", "instant")):
            raise DashboardConfigError("a panel uses either targets or expr/legend/instant")
        targets = _list(value["targets"], "panel.targets", 16)
    else:
        targets = [{key: value[key] for key in ("expr", "legend", "instant") if key in value}]
    if not targets:
        raise DashboardConfigError("panel.targets must contain at least one query")
    fields = deepcopy(_object(value.get("fieldConfig", {}), "panel.fieldConfig"))
    defaults = _object(fields.get("defaults", {}), "panel.fieldConfig.defaults")
    for key in ("unit", "decimals", "min", "max", "thresholds"):
        if key in value and key in defaults:
            raise DashboardConfigError(f"panel.{key} and fieldConfig.defaults.{key} cannot both be set")
        if key in value:
            defaults[key] = deepcopy(value[key])
    if "unit" in defaults:
        _text(defaults["unit"], "panel.unit", 100)
    for key in ("min", "max", "decimals"):
        number = defaults.get(key)
        if key in defaults and (isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number)):
            raise DashboardConfigError(f"panel.{key} must be a finite number")
    if "decimals" in defaults and (not isinstance(defaults["decimals"], int) or not 0 <= defaults["decimals"] <= 20):
        raise DashboardConfigError("panel.decimals must be an integer from 0 to 20")
    fields.update(defaults=defaults, overrides=_list(fields.get("overrides", []), "panel.fieldConfig.overrides", 64))
    default_options = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}} if kind in {"stat", "gauge", "bargauge"} else {}
    return {
        "id": index + 1, "title": _text(value.get("title"), "panel.title", 240),
        "description": _description(value.get("description", ""), "panel.description"), "type": kind,
        "datasource": deepcopy(datasource),
        "gridPos": {"x": (index % 2) * 12, "y": (index // 2) * 8, "w": 12, "h": 8},
        "targets": [_target(target, number, kind, datasource) for number, target in enumerate(targets)],
        "fieldConfig": fields,
        "options": {**default_options, **deepcopy(_object(value.get("options", {}), "panel.options"))},
        "transformations": deepcopy(_list(value.get("transformations", []), "panel.transformations", 32)),
    }


def build_dashboard(config: Mapping[str, Any]) -> dict:
    """Convert a versioned application config into an ordinary Grafana dashboard."""
    value = _object(config, "dashboard")
    _keys(value, {"schema", "uid", "title", "description", "datasource", "refresh", "time", "timezone", "tags", "variables", "panels"}, "dashboard")
    if type(value.get("schema")) is not int or value["schema"] != 1:
        raise DashboardConfigError("dashboard.schema must be 1")
    uid = _text(value.get("uid"), "dashboard.uid", 40)
    if not UID.fullmatch(uid):
        raise DashboardConfigError("dashboard.uid must contain only letters, digits, underscores or hyphens")
    datasource = _datasource(value.get("datasource", "victoria"))
    refresh = value.get("refresh", "30s")
    if not isinstance(refresh, str) or not INTERVAL.fullmatch(refresh):
        raise DashboardConfigError("dashboard.refresh must be empty or an interval such as 30s, 5m or 1h")
    period = _object(value.get("time", {"from": "now-6h", "to": "now"}), "dashboard.time")
    _keys(period, {"from", "to"}, "dashboard.time")
    for key in ("from", "to"):
        _text(period.get(key), f"dashboard.time.{key}", 100)
    variables = [_variable(item, datasource) for item in _list(value.get("variables", []), "dashboard.variables", 32)]
    if len({item["name"] for item in variables}) != len(variables):
        raise DashboardConfigError("variable names must be unique")
    panels = _list(value.get("panels"), "dashboard.panels", 64)
    if not panels:
        raise DashboardConfigError("dashboard.panels must contain at least one panel")
    tags = [_text(tag, "dashboard tag", 100) for tag in _list(value.get("tags", []), "dashboard.tags", 32)]
    result = {
        "id": None, "uid": uid, "title": _text(value.get("title"), "dashboard.title", 240),
        "description": _description(value.get("description", ""), "dashboard.description"), "schemaVersion": 39, "version": 1,
        "editable": False, "timezone": _text(value.get("timezone", "browser"), "dashboard.timezone", 100),
        "refresh": refresh, "time": period, "tags": tags,
        "templating": {"list": variables},
        "panels": [_panel(panel, index, datasource) for index, panel in enumerate(panels)],
    }
    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise DashboardConfigError("dashboard must contain finite JSON values") from error
    if len(encoded) > MAX_BYTES:
        raise DashboardConfigError("rendered dashboard exceeds 1 MiB")
    return result


def write_dashboard(config: Mapping[str, Any], output: str | Path) -> Path:
    """Validate first, then atomically replace one dashboard file for provisioning."""
    dashboard = build_dashboard(config)
    path = Path(output)
    if path.suffix != ".json":
        raise DashboardConfigError("output must use the .json extension for Grafana provisioning")
    body = json.dumps(dashboard, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def main(argv: list[str] | None = None) -> int:
    """Render local configuration without contacting Grafana or handling tokens."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="application-owned JSON configuration")
    parser.add_argument("--output", type=Path, required=True, help="standard Grafana .json file")
    args = parser.parse_args(argv)
    try:
        with args.config.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise DashboardConfigError("input configuration exceeds 1 MiB")
        config = json.loads(raw)
        path = write_dashboard(config, args.output)
    except (OSError, ValueError, UnicodeError) as error:
        parser.error(str(error))
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
