"""Bounded alert source labels shared by Control and the log aggregator."""
from __future__ import annotations

import hashlib
import json
import math
import re

MAX_SOURCES = 16
MAX_SOURCE_BYTES = 8192
MAX_EXCHANGE_BYTES = 262144
SILENCE_SECONDS = (3600, 21600, 86400)


def source(kind, name):
    """Retain exact observable labels; unidentified sources remain unsilenced."""
    if (kind not in ('task', 'service', 'target') or not isinstance(name, str) or not name
            or len(name.encode('utf-8')) > 256 or any(ord(c) < 32 for c in name)):
        return None
    key = hashlib.sha256(json.dumps([kind, name], ensure_ascii=False).encode()).hexdigest()
    return {'key': key, 'kind': kind, 'name': name}


def sample_source(sample):
    """Prefer the SDK task identity and fall back to the deployed service label."""
    if isinstance(sample.get('source_key'), str) and re.fullmatch('[0-9a-f]{64}', sample['source_key']):
        return {'key': sample['source_key']}
    if sample.get('task_name'):
        return source('task', sample['task_name'])
    return source('service', sample.get('task'))


def bounded_sources(rows, total):
    """Merge counts without deriving a mute scope from truncated display text."""
    if not isinstance(rows, list) or not isinstance(total, (int, float)):
        return []
    result = {}
    for row in rows[:MAX_SOURCES + 1]:
        if not isinstance(row, dict):
            continue
        identity = source(row.get('kind'), row.get('name'))
        count = row.get('count')
        if (not identity or isinstance(count, bool) or not isinstance(count, (int, float))
                or not math.isfinite(count) or count <= 0):
            continue
        if identity['key'] not in result:
            if len(result) == MAX_SOURCES:
                continue
            result[identity['key']] = {**identity, 'count': 0}
        result[identity['key']]['count'] += count
    # Inconsistent metadata must never suppress an otherwise valid aggregate.
    if sum(r['count'] for r in result.values()) > total:
        return []
    while len(json.dumps(list(result.values()), ensure_ascii=False).encode()) > MAX_SOURCE_BYTES:
        result.pop(next(reversed(result)))
    return list(result.values())
