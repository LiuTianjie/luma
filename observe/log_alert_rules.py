"""Bounded, service-scoped log rule contract shared by Control and observe."""
from __future__ import annotations

import hashlib
import json
import re

MAX_RULES = 20
INTERVAL = 60
MAX_AGE = 150


def normalize(target, config):
    """Validate declarative conditions; never accept an arbitrary query pipeline."""
    if not isinstance(target, str) or not target.strip() or target == '*' or len(target) > 300:
        raise ValueError('Log rules require one application ID (no wildcard)')
    if not isinstance(config, dict):
        raise ValueError('logQuery must be an object')
    result = {}
    for key, default, limit in [('task', '', 300), ('contains', '', 256), ('exclude', '', 256),
                                ('field', '', 128), ('equals', '', 256)]:
        value = config.get(key, default)
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValueError(f'Invalid logQuery.{key}')
        result[key] = value.strip() if key in ('task', 'field') else value
    window = config.get('windowSeconds', 300)
    if isinstance(window, bool) or not isinstance(window, int) or not 60 <= window <= 900:
        raise ValueError('Log window must be 60–900 seconds')
    result['windowSeconds'] = window
    if result['field'] and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]{0,127}', result['field']):
        raise ValueError('JSON field must contain letters, digits, underscores or dots')
    if not result['contains'] and not (result['field'] and result['equals']):
        raise ValueError('Provide text to match or a JSON field and value')
    if bool(result['field']) != bool(result['equals']):
        raise ValueError('JSON field and value must be provided together')
    if len(json.dumps({'target': target, 'logQuery': result}, ensure_ascii=False).encode()) > 2400:
        raise ValueError('Log rule exceeds the 2400-byte configuration budget')
    return result


def fingerprint(target, config):
    return hashlib.sha256(json.dumps([target, config], sort_keys=True).encode()).hexdigest()


def compile_query(target, config):
    """Use native LogsQL aggregation with exact stream selectors and escaped literals."""
    config = normalize(target, config)
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    selectors = [f'app={quote(target)}']
    if config['task']:
        selectors.append(f'task={quote(config["task"])}')
    base = '{' + ','.join(selectors) + '}'
    conditions = []
    if config['contains']:
        conditions.append('_msg:~' + quote(re.escape(config['contains'])))
    if config['exclude']:
        conditions.append('NOT _msg:~' + quote(re.escape(config['exclude'])))
    if config['field']:
        # Extract only the requested field under a private name, preserving metadata.
        base += f' | unpack_json fields ({config["field"]}) result_prefix "parsed."'
        conditions.append(f'parsed.{config["field"]}:={quote(config["equals"])}')
    condition = ' AND '.join(conditions)
    return base, condition
