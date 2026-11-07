import io
import json
import os
import time
import unittest
import urllib.parse
import urllib.request
from unittest.mock import patch

from luma import log_alert_rules
from luma.control import task_results
from luma.task_results import completion_query, failure_query


class TaskResultTests(unittest.TestCase):
    def test_unavailable_or_no_decided_completions_is_not_zero(self):
        with patch.object(task_results.urllib.request, 'urlopen', side_effect=TimeoutError()):
            self.assertEqual(task_results.signal({'app': 'orders', 'since': 100, 'until': 200})['state'], 'timeout')
        with patch.object(task_results.urllib.request, 'urlopen', return_value=io.BytesIO(b'')):
            self.assertIsNone(task_results.signal({'app': 'orders', 'since': 100, 'until': 200})['value'])

    def test_exception_summary_retains_identity_redacts_and_fits_exchange(self):
        row = {'_msg': json.dumps({'event': 'task.failure.detail', 'trace_id': 'a'*32, 'span_id': 'b'*16,
            'attributes': {'task.name': 'print', 'messaging.message.id': 'm'*100, 'error.location': '/app/file.py:168'},
            'exception': {'error_message': 'HTTP 400 token=private', 'stacktrace': 'Traceback\n' + 'call\n'*1000 + 'RuntimeError: HTTP 400'}}),
            'task': '\\'*128, 'alloc': '\\'*64, '_time': '\\'*40, 'stream': 'stderr'}
        samples = log_alert_rules.bounded_samples([row]*3)
        for sample in samples:
            self.assertEqual(sample['trace_id'], 'a'*32)
            self.assertEqual(sample['span_id'], 'b'*16)
            self.assertNotIn('private', json.dumps(sample))
            self.assertLessEqual(len(json.dumps(sample, ensure_ascii=False).encode()), 900)
        self.assertEqual(log_alert_rules.bounded_samples(samples), samples)


@unittest.skipUnless(os.environ.get('LUMA_TEST_LOGS_URL'), 'requires an isolated VictoriaLogs fixture')
class TaskResultLogsIntegrationTests(unittest.TestCase):
    def test_source_counts_cover_the_window_independently_of_display_samples(self):
        from tests.test_observe import load_observe
        load_observe('alert_sources.py', 'alert_sources')
        load_observe('task_results.py', 'task_results')
        load_observe('log_alert_rules.py', 'log_alert_rules')
        engine = load_observe('log_alerts.py', 'source_result_engine')
        base = os.environ['LUMA_TEST_LOGS_URL']
        now = int(time.time())
        application = 'source-fixture-' + str(time.time_ns())
        rows = [{'_time': now-10, 'app': application, 'task': 'worker', '_msg': json.dumps({
            'event': 'business_outcome' if name == 'jobs.beta' else 'task.finished', 'attributes': {'task.name': name, 'task.business.outcome': 'needs_review'}})}
            for name in ['jobs.alpha'] * 5 + ['jobs.beta']]
        rows.append({'_time': now-10, 'app': application, 'task': 'legacy', '_msg': 'needs_review'})
        request = urllib.request.Request(base + '/insert/jsonline?_stream_fields=app,task',
            data='\n'.join(json.dumps(row) for row in rows).encode(), headers={'Content-Type': 'application/stream+json'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
        # Publish this isolated fixture deterministically before checking queries.
        with urllib.request.urlopen(urllib.request.Request(base + '/internal/force_flush', data=b'')) as response:
            self.assertEqual(response.status, 200)
        urlopen = urllib.request.urlopen

        def local(request, **kwargs):
            url = request if isinstance(request, str) else request.full_url
            if url.endswith('/coverage'):
                return io.BytesIO(json.dumps([{'app': application, 'task': task, 'at': now} for task in ('worker', 'legacy')]).encode())
            return urlopen(urllib.request.Request(url.replace('http://127.0.0.1:9428', base), data=request.data), **kwargs)

        with patch.object(urllib.request, 'urlopen', side_effect=local):
            for query, expected in [({'contains': 'needs_review'}, {('task', 'jobs.alpha'): 5, ('task', 'jobs.beta'): 1, ('service', 'legacy'): 1}),
                                    ({'field': 'attributes.task.business.outcome', 'equals': 'needs_review'}, {('task', 'jobs.alpha'): 5, ('task', 'jobs.beta'): 1})]:
                result = engine.query_logs({'target': application, 'logQuery': log_alert_rules.normalize(application, query)}, now)
                self.assertEqual(result['value'], sum(expected.values()))
                self.assertEqual({(item['kind'], item['name']): item['count'] for item in result['sources']}, expected)
                self.assertLessEqual(len(result['samples']), 3)
            now += 61
            empty = engine.query_logs({'target': application, 'logQuery': log_alert_rules.normalize(application, {'contains': 'needs_review', 'windowSeconds': 60})}, now)
            self.assertEqual(empty['status'], 'no_source_logs')
            self.assertIsNone(empty['value'])

    def test_business_execution_overlap_retry_dedup_and_scopes(self):
        base = os.environ['LUMA_TEST_LOGS_URL']
        now = int(time.time())
        rows = []
        cases = [('succeeded', 'failed'), ('failed', ''), ('failed', 'failed'),
                 ('retrying', ''), ('cancelled', ''), ('succeeded', 'needs_review'),
                 ('succeeded', ''), ('succeeded', 'succeeded')]
        for index, (framework, business) in enumerate(cases):
            payload = {'event': 'task.finished', 'trace_id': f'{index//2:032x}', 'span_id': f'{index:016x}',
                       'attributes': {'task.name': 'print', 'task.framework': 'taskiq', 'task.queue': 'critical',
                                      'outcome': framework, 'task.business.outcome': business,
                                      'messaging.message.id': 'same-business-id'}}
            rows.append({'_time': now-10, '_msg': json.dumps(payload), 'app': 'result-fixture',
                         'task': 'worker', 'service_name': 'result-worker', 'environment': 'production'})
        # Duplicate ingestion, a failure detail and another app never inflate totals.
        detail = {**json.loads(rows[0]['_msg']), 'event': 'task.failure.detail',
                  'exception': {'error_message': 'HTTP 400 token=private', 'stacktrace': 'Traceback\n  File /app/print.py:168\nRuntimeError: HTTP 400'}}
        rows += [rows[0], {**rows[0], '_msg': json.dumps(detail)}, {**rows[0], 'app': 'other'}]
        request = urllib.request.Request(base + '/insert/jsonline?_stream_fields=app,task',
                                         data='\n'.join(json.dumps(row) for row in rows).encode(), headers={'Content-Type': 'application/stream+json'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)

        def query(expr):
            data = urllib.parse.urlencode({'query': expr, 'start': now-30, 'end': now, 'timeout': '2s'}).encode()
            with urllib.request.urlopen(urllib.request.Request(base+'/select/logsql/query', data=data)) as response:
                return [json.loads(line) for line in response.read().splitlines() if line]

        with urllib.request.urlopen(urllib.request.Request(base + '/internal/force_flush', data=b'')) as response:
            self.assertEqual(response.status, 200)
        result = query(completion_query('result-fixture', {'service': 'result-worker', 'environment': 'production'}))
        self.assertEqual(len(result), 1)
        self.assertEqual({key: int(result[0][key]) for key in ('failed', 'succeeded', 'retrying', 'cancelled', 'unknown')},
                         {'failed': 3, 'succeeded': 2, 'retrying': 1, 'cancelled': 1, 'unknown': 1})
        self.assertEqual(query(completion_query('result-fixture', {'service': 'absent'})), [])
        failed = query(failure_query('result-fixture'))[0]
        self.assertEqual(float(failed['matches']), 3)
        self.assertEqual(float(failed['total']), 8)
        self.assertEqual(len(json.loads(failed['attempts'])), 3)

        from tests.test_observe import load_observe
        load_observe('alert_sources.py', 'alert_sources')
        load_observe('task_results.py', 'task_results')
        load_observe('log_alert_rules.py', 'log_alert_rules')
        engine = load_observe('log_alerts.py', 'task_result_engine')
        urlopen = urllib.request.urlopen
        def local(request, **kwargs):
            url = request if isinstance(request, str) else request.full_url
            if url.endswith('/coverage'):
                return io.BytesIO(json.dumps([{'app': 'result-fixture', 'task': 'worker', 'at': now}]).encode())
            return urlopen(urllib.request.Request(url.replace('http://127.0.0.1:9428', base), data=request.data), **kwargs)
        with patch.object(urllib.request, 'urlopen', side_effect=local):
            totals = task_results.read({'app': 'result-fixture', 'since': now-30, 'until': now})
            self.assertEqual(totals['counts']['failed'], 3)
            self.assertEqual(totals['ratio'], 3/5)
            result = engine.query_logs({'target': 'result-fixture', 'logQuery': log_alert_rules.normalize('result-fixture', {'mode': 'task_failures'})}, now)
        self.assertEqual(result['value'], 3)
        self.assertEqual(len(result['samples']), 1)
        self.assertTrue(any('RuntimeError: HTTP 400' in row['_msg'] for row in result['samples']))
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual([(item['kind'], item['name'], item['count']) for item in result['sources']], [('task', 'print', 3)])
