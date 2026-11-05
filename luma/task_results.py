"""Task completion semantics shared by dashboard totals and log alert rules.

Count retained SDK completion records, never sampled spans or two overlapping
framework/business counters. One trace/span pair is one execution attempt.
"""
from __future__ import annotations

import json

GROUP = ('sdk.attributes.task.name', 'sdk.attributes.task.framework', 'sdk.attributes.task.queue')
FAILED = '(sdk.attributes.task.business.outcome:="failed" OR sdk.attributes.outcome:="failed")'
RETRYING = '(sdk.attributes.task.business.outcome:="retrying" OR sdk.attributes.outcome:="retrying")'
CANCELLED = '(sdk.attributes.task.business.outcome:="cancelled" OR sdk.attributes.outcome:="cancelled")'
SUCCEEDED = '(sdk.attributes.outcome:="succeeded" AND (sdk.attributes.task.business.outcome:="" OR sdk.attributes.task.business.outcome:="succeeded"))'


def completion_base(app, filters=None):
    filters = filters or {}
    selectors = ['app=' + json.dumps(app)]
    if filters.get('task'):
        selectors.append('task=' + json.dumps(filters['task']))
    base = '{' + ','.join(selectors) + '}'
    for key, field in (('environment', 'environment'), ('service', 'service_name'), ('instance', 'service_instance_id')):
        if filters.get(key):
            base += f' {field}:={json.dumps(filters[key])}'
    # VictoriaLogs 1.25 prunes explicitly selected unpack fields incorrectly
    # before multi-stage stats. Full unpack preserves the exact predicates.
    return (base + ' | unpack_json result_prefix "sdk."'
            ' | filter sdk.event:="task.finished" AND sdk.trace_id:~"^[0-9a-f]{32}$" AND sdk.span_id:~"^[0-9a-f]{16}$"')


def attempt_stats(base, *, samples=False):
    query = base + ' | stats by (sdk.trace_id, sdk.span_id, ' + ', '.join(GROUP) + ') '
    query += ', '.join(f'count() if ({condition}) as {name}' for name, condition in (
        ('failed', FAILED), ('retrying', RETRYING), ('cancelled', CANCELLED), ('succeeded', SUCCEEDED)))
    if samples:
        query += ', json_values(_time, _msg, task, alloc, stream) limit 1 if (' + FAILED + ') as samples'
    return query


def completion_query(app, filters=None):
    query = attempt_stats(completion_base(app, filters))
    return query + ' | stats by (' + ', '.join(GROUP) + ') ' + ', '.join([
        'count() if (failed:>0) as failed',
        'count() if (failed:=0 AND retrying:>0) as retrying',
        'count() if (failed:=0 AND retrying:=0 AND cancelled:>0) as cancelled',
        'count() if (failed:=0 AND retrying:=0 AND cancelled:=0 AND succeeded:>0) as succeeded',
        'count() if (failed:=0 AND retrying:=0 AND cancelled:=0 AND succeeded:=0) as unknown',
    ]) + ' | limit 2001'


def failure_query(app, filters=None):
    # Count every observed attempt once; details are selected only as evidence.
    return (attempt_stats(completion_base(app, filters), samples=True)
            + ' | stats count() as total, count() if (failed:>0) as matches, '
            'json_values(samples) limit 3 if (failed:>0) as attempts')
