"""Container memory alerts from Agent samples, grouped by Nomad application."""
from __future__ import annotations

from .monitoring import SAMPLE_TTL_SECONDS, _number


def hydrate(state):
    """Resolve allocation IDs once, outside the evaluator's SQLite transaction.

    Keep Compose tasks under their job/stack name. Dashboard resource summaries
    expand tasks and average limits, which would hide a single hot container.
    """
    from .server import _control_config_path, _nomad_allocation_service_index, load_config

    try:
        index = _nomad_allocation_service_index(load_config(_control_config_path()), state)
    except Exception:
        index = {}
    state['_memoryAllocationIndex'] = index


def observations(state, now, threshold):
    from .server import _lookup_nomad_allocation

    index = state.get('_memoryAllocationIndex') or {}
    values, incomplete = {}, set()
    unresolved = False
    for node in (state.get('nodes') or {}).values():
        agent = node.get('agent') if isinstance(node, dict) else None
        if not isinstance(agent, dict):
            continue
        seen = _number(agent.get('lastSeen'))
        sampled = _number(agent.get('containerStatsCollectedAt'))
        fresh = (seen is not None and sampled is not None
                 and 0 <= now - seen <= SAMPLE_TTL_SECONDS
                 and 0 <= now - sampled <= SAMPLE_TTL_SECONDS)
        containers = agent.get('containerStats')
        for item in containers if isinstance(containers, list) else []:
            if not isinstance(item, dict):
                continue
            service = str(item.get('service') or '').strip()
            allocation = str(item.get('nomadAllocId') or item.get('allocationId') or '').strip()
            if service.startswith('nomad:') and not allocation:
                allocation = service.split(':', 1)[1]
            if allocation:
                service = _lookup_nomad_allocation(index, allocation).get('service') or service
            if not service or service.startswith('nomad:'):
                # Its application is unknown: a cool known replica cannot prove
                # recovery, but a known hot replica still proves a breach.
                unresolved = True
                continue
            values.setdefault(service, [])
            limit = _number(item.get('memoryLimitBytes'))
            percent = _number(item.get('memoryPercent'))
            if 'memoryPercent' not in item:
                used = _number(item.get('memoryUsageBytes'))
                percent = _number(100 * used / limit) if used is not None and limit else None
            if fresh and limit and percent is not None:
                values[service].append(percent)
            else:
                incomplete.add(service)
    result = {}
    for service, samples in values.items():
        value = max(samples) if samples else None
        # A partial maximum above the threshold is conclusive; a partial
        # maximum below it is not. Preserve noData semantics in the evaluator.
        result[service] = value if value is not None and (
            value > threshold or (service not in incomplete and not unresolved)
        ) else None
    return result
