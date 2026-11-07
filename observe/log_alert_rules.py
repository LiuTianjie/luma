"""Bounded, service-scoped log rule contract shared by Control and observe."""
from __future__ import annotations

import hashlib
import json
import re

if __package__:
    from .alert_sources import sample_source, source
else:
    from alert_sources import sample_source, source

MAX_RULES = 20
INTERVAL = 60
MAX_AGE = 150


def clean_sample_text(value, limit=512):
    """Bound UTF-8 bytes and redact common credentials before snapshots leave observe."""
    text = str(value or '')
    text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)
    text = re.sub(r'(https?://)[^/\s@]+@', r'\1[redacted]@', text)
    # Redact the complete bearer value before the key/value pass can consume
    # only the word "Bearer" and leave its credential behind.
    text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [redacted]', text)
    text = re.sub(r'(?i)(\b(?:password|token|secret|authorization|api[_-]?key|access_token)\b[\s"\x27]*[:=][\s"\x27]*)([^\s,;"\x27]+)', r'\1[redacted]', text)
    data = text.encode('utf-8')
    if len(data) > limit:
        return data[:limit//3].decode('utf-8', 'ignore') + '\n…\n' + data[-(limit*2//3-8):].decode('utf-8', 'ignore')
    return text


def bounded_samples(rows):
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows[:3]:
        if not isinstance(row, dict):
            continue
        row = execution_sample(row)
        sample = {key: clean_sample_text(row.get(key), limit) for key, limit in
                  [('_msg', 512), ('_time', 40), ('task', 128), ('alloc', 64), ('stream', 16)]}
        for key, length in [('trace_id', 32), ('span_id', 16), ('task_id', 100)]:
            if row.get(key):
                sample[key] = clean_sample_text(row[key], length)
        if source('task', row.get('task_name')):
            sample['task_name'] = row['task_name']
        identity = sample_source(row)
        if identity:
            sample['source_key'] = identity['key']
        # Account for JSON escaping, not just character counts, so 20 rules
        # fit the exchange budget alongside source counts even with adversarial messages.
        while len(json.dumps(sample, ensure_ascii=False).encode()) > 900:
            key = max(('_msg', 'task', 'task_id'), key=lambda k: len(json.dumps(sample.get(k, ''), ensure_ascii=False).encode()))
            budget = len(sample.get(key, '').encode()) // 2
            sample[key] = clean_sample_text(sample[key], budget) if budget >= 16 else ''
        result.append(sample)
    return result


def execution_sample(row):
    """Extract identity before truncation and show the error rather than raw JSON."""
    try:
        payload = json.loads(row.get('_msg', ''))
    except (ValueError, TypeError):
        # Recover exact IDs from the old head/tail JSON snapshot format. The
        # full-log reader still verifies every ID against a structured record.
        raw = str(row.get('_msg', ''))
        if raw.startswith('{') and '\n…\n' in raw:
            row = dict(row)
            for key, length in [('trace_id', 32), ('span_id', 16)]:
                matches = re.findall(r'"' + key + r'"\s*:\s*"([0-9a-f]{' + str(length) + r'})"', raw)
                if len(matches) == 1:
                    row[key] = matches[0]
        return row
    if not isinstance(payload, dict):
        return row
    attrs = payload.get('attributes')
    if not isinstance(attrs, dict):
        return row
    row = {**row, 'task_name': attrs.get('task.name', '')}
    if not str(payload.get('event', '')).startswith('task.'):
        return row
    exception = payload.get('exception') or {}
    if not isinstance(exception, dict):
        exception = {}
    parts = [attrs.get('task.name'), exception.get('error_message') or attrs.get('error.message'),
             attrs.get('error.location'), exception.get('stacktrace')]
    if not parts[1]:
        parts[1] = attrs.get('error.type') or payload.get('event')
    sample = {**row, '_msg': '\n'.join(str(part) for part in parts if part)}
    for key, length in [('trace_id', 32), ('span_id', 16)]:
        value = payload.get(key, '')
        if isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % length, value):
            sample[key] = value
    sample['task_id'] = attrs.get('messaging.message.id', '')
    sample['task_name'] = attrs.get('task.name', '')
    return sample


def normalize(target, config):
    """Validate declarative conditions; never accept an arbitrary query pipeline."""
    if not isinstance(target, str) or not target.strip() or target == '*' or len(target) > 300:
        raise ValueError('Log rules require one application ID (no wildcard)')
    if not isinstance(config, dict):
        raise ValueError('logQuery must be an object')
    result = {}
    mode = config.get('mode', 'lines')
    if mode not in ('lines', 'task_failures'):
        raise ValueError('Invalid logQuery.mode')
    if mode != 'lines':
        result['mode'] = mode
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
    if mode == 'task_failures' and any(result[k] for k in ('contains', 'exclude', 'field', 'equals')):
        raise ValueError('Task failure rules use the fixed business-or-execution failure condition')
    if mode == 'lines' and not result['contains'] and not (result['field'] and result['equals']):
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
