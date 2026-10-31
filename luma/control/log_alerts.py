"""Exchange bounded rule snapshots with observe; never read or scan logs here."""
from __future__ import annotations

import json
import time
import urllib.request

from ..log_alert_rules import MAX_AGE, MAX_RULES, bounded_samples, fingerprint, normalize
from ..errors import LumaError


def request(path, body):
    raw = json.dumps(body, ensure_ascii=False).encode()
    if len(raw) > 65536:
        return {}
    req = urllib.request.Request('http://127.0.0.1:9109/'+path, data=raw,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=0.5) as response:
            data = response.read(65537)
        if len(data) > 65536:
            return {}
        result = json.loads(data)
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def samples(rules, now):
    if not rules:
        return {}
    rows = request('sync', {'rules': [{'target': r['target'], 'logQuery': r['logQuery']} for r in rules[:MAX_RULES]]}).get('results', {})
    if not isinstance(rows, dict):
        return {}
    values = {}
    for rule in rules:
        key = fingerprint(rule['target'], rule['logQuery'])
        row = rows.get(key)
        if isinstance(row, dict) and isinstance(row.get('sampledAt'), (int, float)) and 0 <= now-row['sampledAt'] <= MAX_AGE:
            values[rule['id']] = {**row, 'samples': bounded_samples(row.get('samples', []))} if row.get('status') == 'ok' else None
    return values


def preview(body):
    try:
        config = normalize(body.get('target'), body.get('logQuery'))
    except ValueError as exc:
        raise LumaError(str(exc)) from exc
    result = request('preview', {'target': body['target'], 'logQuery': config})
    return result or {'status': 'unavailable', 'sampledAt': time.time()}
