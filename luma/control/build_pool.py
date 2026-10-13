"""Explicit build-node enrollment, separate from legacy inferred builder roles."""
from __future__ import annotations

from ..errors import LumaError


def declared_names(state):
    raw = (state.get('build') or {}).get('nodes') or []
    if isinstance(raw, dict):
        raw = [name for name, enabled in raw.items() if enabled]
    return list(dict.fromkeys(str(name).strip() for name in raw if str(name).strip()))


def inventory(state):
    from . import server as s
    registered = state.get('nodes') or {}
    result = []
    for name in declared_names(state):
        record = s._node_record_for_name(registered, name)
        if not record:
            result.append({'name': name, 'nodeId': '', 'platform': '', 'ready': False})
            continue
        platform = s._nomad_node_platform_from_record(record)
        node_id = s._node_record_nomad_node_id(record)
        result.append({'name': name, 'nodeId': node_id, 'platform': platform,
                       'region': record.get('region', ''),
                       'ready': bool(node_id and platform in {'linux/amd64', 'linux/arm64'})})
    return result


def validate_nodes(state, names):
    from . import server as s
    registered = state.get('nodes') or {}
    if len(names) > 100:
        raise LumaError('at most 100 build nodes may be selected')
    for name in names:
        record = s._node_record_for_name(registered, name)
        if not record:
            raise LumaError('unknown Luma node: ' + name)
        platform = s._nomad_node_platform_from_record(record)
        if platform not in {'linux/amd64', 'linux/arm64'}:
            raise LumaError('build node must have a known native Linux amd64/arm64 architecture: ' + name)
        if not s._node_record_nomad_node_id(record):
            raise LumaError('build node has no registered Nomad identity: ' + name)


def target_inventory(state):
    """Architecture is durable identity, not an inference from agent readiness."""
    from . import server as s
    by_node, by_region = {}, {}
    for key, record in (state.get('nodes') or {}).items():
        if not isinstance(record, dict):
            continue
        platform = s._container_platform_for_node(record)
        region = str(record.get('region') or '')
        for name in s._node_record_names(str(key), record):
            by_node[name] = platform
        if region:
            by_region.setdefault(region, set()).add(platform)
    return {'byNode': by_node, 'byRegion': {region: sorted(platforms) for region, platforms in by_region.items()}}


def candidates(state):
    from . import server as s
    names = []
    for key, record in (state.get('nodes') or {}).items():
        if isinstance(record, dict) and s._nomad_node_platform_from_record(record) in {'linux/amd64', 'linux/arm64'} and s._node_record_nomad_node_id(record):
            names.append(str(record.get('name') or key))
    return inventory({**state, 'build': {'nodes': names}})
