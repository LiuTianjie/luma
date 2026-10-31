import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luma import log_alert_rules as contract
from luma.control import alerting, log_alerts
from luma.errors import LumaError
from tests.test_observe import load_observe


class LogRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'LUMA_CONTROL_STATE_DIR': self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def rule(self, **extra):
        return alerting.save_rule(dict(name='Worker failures', metric='app.log_matches', target='ledger',
                                      threshold=4, forSeconds=0, logQuery={'contains': 'failed'}, **extra), now=1000)

    def test_configuration_and_incident_lifecycle(self):
        rule = self.rule()
        alerting.tick({'_logAlerts': {rule['id']: 5}}, now=1000)
        self.assertEqual(alerting.dispatch('GET', 'incidents')['items'][0]['status'], 'firing')
        alerting.tick({'_logAlerts': {}}, now=1015)
        incident = alerting.dispatch('GET', 'incidents')['items'][0]
        self.assertEqual(incident['status'], 'firing')
        self.assertTrue(incident['noData'])
        alerting.tick({'_logAlerts': {rule['id']: 0}}, now=1030)
        self.assertEqual(alerting.dispatch('GET', 'incidents')['items'][0]['status'], 'resolved')
        # Editing a different predicate cannot reuse the previous observation.
        alerting.tick({'_logAlerts': {rule['id']: 5}}, now=1045)
        edited = alerting.save_rule({**rule, 'logQuery': {'contains': 'fatal'}}, now=1050)
        self.assertEqual(edited['logQuery']['contains'], 'fatal')
        self.assertTrue(all(i['status'] in ('resolved', 'closed') for i in alerting.dispatch('GET', 'incidents')['items']))

    def test_bounded_configuration_and_literal_queries(self):
        for target, config in [('*', {'contains':'error'}), ('app', {'contains':'error', 'windowSeconds':901}),
                               ('app', {'field':'level | stats count()', 'equals':'error'})]:
            with self.assertRaises(ValueError):
                contract.normalize(target, config)
        base, condition = contract.compile_query('app"}', {'contains':'a.* | stats count()', 'field':'severity', 'equals':'error'})
        self.assertIn('app="app\\\"}"', base)
        self.assertIn('parsed.severity:=', condition)
        for _ in range(contract.MAX_RULES):
            self.rule()
        with self.assertRaises(LumaError):
            self.rule()

    def test_control_ignores_stale_samples_and_never_receives_full_logs(self):
        rule = self.rule()
        key = contract.fingerprint(rule['target'], rule['logQuery'])
        with patch.object(log_alerts, 'request', return_value={'results': {key: {'status':'ok','value':8,'sampledAt':1000}}}):
            self.assertEqual(log_alerts.samples([rule], 1050), {rule['id']:8})
            self.assertEqual(log_alerts.samples([rule], 1200), {})

    def test_configuration_overflow_cannot_break_other_alerts(self):
        with self.assertRaises(ValueError):
            contract.normalize('app', {'contains':'错'*256, 'exclude':'误'*256, 'equals':'值'*256, 'field':'level', 'task':'服'*300})
        self.assertEqual(log_alerts.request('sync', {'rules':['x'*65537]}), {})

    def test_observe_build_contract_is_identical(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual((root/'luma/log_alert_rules.py').read_bytes(), (root/'observe/log_alert_rules.py').read_bytes())


class LogEngineTest(unittest.TestCase):
    def setUp(self):
        load_observe('log_alert_rules.py', 'log_alert_rules')
        self.mod = load_observe('log_alerts.py', 'observe_log_alerts')

    def test_serial_budget_expiry_and_error(self):
        now = [1000]
        calls = []
        def query(rule, stamp):
            calls.append(rule)
            if rule['target'] == 'broken':
                raise TimeoutError()
            return {'status':'ok','value':2,'sampledAt':stamp}
        engine = self.mod.Engine(query, lambda: now[0])
        rules = [{'target':t,'logQuery':{'contains':'error'}} for t in ['one', 'broken']]
        self.assertEqual(engine.exchange(rules), {'results':{}})
        engine.step()
        self.assertEqual(len(calls), 1)
        engine.step()
        self.assertEqual(len(calls), 2)
        engine.step()
        self.assertEqual(len(calls), 2)
        self.assertIn('unavailable', [r['status'] for r in engine.results.values()])
        now[0] = 1200
        engine.step()
        self.assertEqual(engine.results, {})
        self.assertEqual(engine.rules, {})

    def test_preview_is_asynchronous_coalesced_and_read_only(self):
        engine = self.mod.Engine(lambda rule, now: {'status':'ok','value':3,'sampledAt':now}, lambda:1000)
        body = {'target':'app','logQuery':{'contains':'failed'}}
        self.assertEqual(engine.request_preview(body)['status'], 'pending')
        self.assertEqual(engine.request_preview({'target':'other','logQuery':{'contains':'failed'}})['status'], 'busy')
        engine.step()
        self.assertEqual(engine.request_preview(body)['value'], 3)
        self.assertEqual(engine.rules, {})

    def test_read_only_root_is_rendered(self):
        from tests.test_nomad_compose import cfg, write_deployment
        from luma.nomad_render import render_compose_job
        compose = 'services:\n  alerts:\n    image: test\n    network_mode: host\n    read_only: true\n'
        sidecar = 'name: observe\ncompose: docker-compose.yml\nregion: cn\nservices:\n  alerts:\n    exposure: none\n'
        with tempfile.TemporaryDirectory() as directory, patch('tests.test_nomad_compose.tempfile.mkdtemp', return_value=directory):
            job = render_compose_job(cfg(), write_deployment(sidecar, compose), as_json=False)['Job']
        task = job['TaskGroups'][0]['Tasks'][0]
        self.assertTrue(task['Config']['readonly_rootfs'])

    def test_query_does_not_treat_missing_coverage_as_recovery(self):
        import io
        with patch.object(self.mod.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps([]).encode())):
            with self.assertRaises(ValueError):
                self.mod.query_logs({'target':'app','logQuery':{'contains':'error','task':'','windowSeconds':300}},1000)


class LogCoverageTest(unittest.TestCase):
    def test_export_failure_invalidates_source_coverage(self):
        mod = load_observe('log_shipper.py', 'coverage_shipper')
        fail = [False]
        def insert(rows):
            if fail[0]:
                raise OSError('offline')
        size = [2]
        shipper = mod.Shipper(
            list_allocs=lambda:[{'ID':'a','JobID':'app','ClientStatus':'running','TaskStates':{'worker':{}}}],
            list_files=lambda alloc:[{'Name':f'worker.{stream}.0','Size':size[0]} for stream in ('stdout','stderr')],
            readat=lambda *a:b'x\n', insert=insert)
        shipper.poll()
        self.assertGreater(shipper.coverage_snapshot()[0]['at'],0)
        size[0] = 4
        fail[0] = True
        shipper.poll()
        self.assertEqual(shipper.coverage_snapshot()[0]['at'],0)
