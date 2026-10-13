"""Native platform policy shared by planning, scheduling and executor startup."""
from __future__ import annotations

import platform as host_platform

from ..errors import LumaError

SUPPORTED = {'linux/amd64', 'linux/arm64'}


def resolve_platform(context, *, node='', region='', requested=''):
    explicit = {value.strip() for value in str(requested or '').split(',') if value.strip()}
    if explicit - SUPPORTED:
        raise LumaError('only native linux/amd64 and linux/arm64 builds are supported')
    if node:
        target = {context.get('targetPlatformsByNode', {}).get(str(node), '')}
    elif region:
        target = set(context.get('targetPlatformsByRegion', {}).get(str(region), []))
    else:
        target = explicit
    if not target or target - SUPPORTED:
        raise LumaError(f'cannot determine deployment target architecture for {node or region or "this request"}; no architecture fallback is allowed')
    if explicit and not target.issubset(explicit):
        raise LumaError('requested build platform does not cover deployment target architecture')
    return ','.join(sorted(target))


def selected_nodes(nodes, platform, explicit=''):
    if platform not in SUPPORTED:
        raise LumaError('unsupported native build platform')
    if explicit and explicit != 'auto':
        matching = [node for node in nodes if node['name'] == explicit]
        if not matching:
            raise LumaError('build node is not in the configured build-node list: ' + explicit)
        if matching[0].get('platform') != platform:
            raise LumaError('selected build node does not match ' + platform + ': ' + explicit)
        nodes = matching
    return [node for node in nodes if node.get('platform') == platform and node.get('nodeId')]


def verify_native(platform, host_arch, *, machine=None, system=None):
    machine = machine or host_platform.machine()
    system = system or host_platform.system()
    architecture = {'x86_64': 'amd64', 'aarch64': 'arm64', 'AMD64': 'amd64'}.get(machine, machine)
    if platform not in SUPPORTED or system.lower() != 'linux' or platform != 'linux/' + architecture or host_arch != architecture:
        raise LumaError('executor, Nomad host and requested architecture must match; emulated builds are disabled')
