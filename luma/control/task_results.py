"""Bounded read of completed attempts from the existing retained log store."""
from __future__ import annotations

import json
import math
import threading
import urllib.error
import urllib.parse
import urllib.request

from ..task_results import GROUP, completion_query

SLOTS = threading.BoundedSemaphore(2)
MAX_BODY = 512 * 1024


def read(query):
    if not SLOTS.acquire(blocking=False):
        return {'state': 'rate_limited', 'rows': []}
    try:
        params = urllib.parse.urlencode({'query': completion_query(query['app'], query),
                                        'start': query['since'], 'end': query['until'], 'timeout': '2s'})
        request = urllib.request.Request('http://127.0.0.1:9428/select/logsql/query', data=params.encode())
        with urllib.request.urlopen(request, timeout=3) as response:
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            return {'state': 'response_limit', 'rows': []}
        groups = [json.loads(line) for line in raw.splitlines() if line]
        if len(groups) > 2000:
            return {'state': 'response_limit', 'rows': []}
        rows = []
        totals = dict.fromkeys(('succeeded', 'failed', 'retrying', 'cancelled', 'unknown'), 0)
        for group in groups:
            labels = {name: group.get(field, '') for name, field in zip(('task_name', 'task_framework', 'task_queue'), GROUP)}
            for outcome in totals:
                value = float(group[outcome])
                if not math.isfinite(value) or value < 0 or not value.is_integer():
                    raise ValueError('invalid task count')
                totals[outcome] += int(value)
                rows.append({'metric': {**labels, 'outcome': outcome}, 'value': [query['until'], str(int(value))]})
        denominator = totals['succeeded'] + totals['failed']
        return {'state': 'ok' if groups else 'empty', 'rows': rows, 'counts': totals,
                'ratio': totals['failed'] / denominator if denominator else None,
                'source': 'completion_logs'}
    except urllib.error.HTTPError as exc:
        return {'state': 'rate_limited' if exc.code == 429 else 'upstream_error', 'rows': []}
    except TimeoutError:
        return {'state': 'timeout', 'rows': []}
    except (OSError, urllib.error.URLError):
        return {'state': 'unreachable', 'rows': []}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {'state': 'invalid_response', 'rows': []}
    finally:
        SLOTS.release()


def signal(query):
    result = read(query)
    value = result.get('ratio')
    return {'state': 'empty' if result['state'] == 'ok' and value is None else result['state'],
            'value': value, 'counts': result.get('counts'), 'source': 'completion_logs'}
