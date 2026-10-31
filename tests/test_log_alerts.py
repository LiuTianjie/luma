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
            self.assertEqual(log_alerts.samples([rule], 1050)[rule['id']]['value'], 8)
            self.assertEqual(log_alerts.samples([rule], 1200), {})

    def test_evidence_survives_recovery_and_notifies_from_the_same_snapshot(self):
        channel = alerting.save_channel({'name':'group','appId':'cli_test1234','appSecret':'private','chatId':'oc_group1234'})
        rule = self.rule(channelIds=[channel['id']], repeatSeconds=60)
        sample = {'status':'ok','value':5,'total':30,'sampledAt':1000,
                  'samples':[{'_time':'2026-10-31T04:00:00Z','task':'worker','_msg':'Task charge failed: api unauthorized token=private'}]}
        state = {'domain':'luma.test','_logAlerts':{rule['id']:sample}}
        alerting.tick(state, now=1000)
        item = alerting.dispatch('GET','incidents')['items'][0]
        self.assertNotIn('samples', item['logDetails'])
        identifier = item['id']
        details = alerting.dispatch('GET',f'incidents/{identifier}')['incident']['logDetails']
        self.assertEqual(details['matchCount'],5)
        self.assertIn('api unauthorized',details['samples'][0]['_msg'])
        self.assertNotIn('private',json.dumps(details))
        self.assertEqual(details['incidentUrl'],f'https://luma.test/dashboard/apps/ledger/alerts/{identifier}')
        sent=[]
        alerting.deliver_pending(now=1000,transport=lambda *args:sent.append(json.loads(args[3])))
        self.assertIn('api unauthorized',sent[0]['fallback'])
        self.assertIn('api unauthorized',json.dumps(sent[0]['card']))
        alerting.tick({'_logAlerts':{}},now=1015)
        self.assertTrue(alerting.dispatch('GET',f'incidents/{identifier}')['incident']['noData'])
        alerting.tick({'_logAlerts':{rule['id']:{**sample,'value':0,'samples':[],'sampledAt':1060}}},now=1060)
        recovered = alerting.dispatch('GET',f'incidents/{identifier}')['incident']
        self.assertEqual(recovered['status'],'resolved')
        self.assertEqual(recovered['logDetails'],details)
        alerting.deliver_pending(now=1060,transport=lambda *args:sent.append(json.loads(args[3])))
        self.assertIn('api unauthorized',sent[-1]['fallback'])
        self.assertIn('统计窗口内错误',json.dumps(sent[-1]['card'],ensure_ascii=False))

    def test_application_filter_scopes_counts_and_pages_before_limit(self):
        first = self.rule()
        second = alerting.save_rule({'name':'Other','metric':'app.nomad_failed','target':'other','threshold':0,'forSeconds':0})
        node = alerting.save_rule({'name':'Node','metric':'node.disk','target':'ledger','threshold':0,'forSeconds':0})
        alerting.tick({'_logAlerts':{first['id']:5}},now=1000)
        alerting.tick({'_logAlerts':{first['id']:0}},now=1015)
        alerting.tick({'_logAlerts':{first['id']:5},'_observeAlerts':{'app.nomad_failed':{'other':1}},
                       'nodes':{'ledger':{'agent':{'lastSeen':1030,'metricsCollectedAt':1030,'metrics':{'diskUsedPercent':95}}}}},now=1030)
        query={'app':'ledger','limit':1}
        page = alerting.dispatch('GET','incidents',query=query)
        self.assertEqual(page['items'][0]['ruleId'],first['id'])
        older = alerting.dispatch('GET','incidents',query={**query,'cursor':page['nextCursor']})
        self.assertEqual(older['items'][0]['status'],'resolved')
        self.assertIsNone(older['nextCursor'])
        overview = alerting.dispatch('GET','overview',query={'app':'ledger'})
        self.assertEqual(overview['counts']['firing'],1)
        self.assertEqual(overview['counts']['resolved'],1)
        self.assertEqual(overview['enabledRules'],1)
        self.assertEqual([r['id'] for r in alerting.dispatch('GET','rules',query={'app':'ledger'})['items']],[first['id']])
        with self.assertRaises(LumaError):
            alerting.dispatch('GET',f"incidents/{page['items'][0]['id']}",query={'app':'other'})
        self.assertNotEqual(second['id'],node['id'])

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

    def test_victorialogs_json_string_rows_are_decoded_before_redaction(self):
        import io
        packed = [json.dumps({'_msg':f'failed {i} token=private','task':'worker','_time':'2026-10-31T04:00:00Z'}) for i in range(3)]
        responses = [io.BytesIO(json.dumps([{'app':'app','task':'worker','at':1000}]).encode()),
                     io.BytesIO(json.dumps({'total':'10','matches':'5','samples':json.dumps(packed)}).encode())]
        with patch.object(self.mod.urllib.request,'urlopen',side_effect=responses):
            result = self.mod.query_logs({'target':'app','logQuery':contract.normalize('app',{'contains':'failed'})},1000)
        self.assertEqual(result['value'],5)
        self.assertEqual(len(result['samples']),3)
        self.assertTrue(all('private' not in row['_msg'] for row in result['samples']))

    def test_twenty_large_sample_sets_fit_exchange_budget(self):
        row = {'_msg': '\x00' * 300 + 'failed ' + '\\' * 9000 + '尾巴' * 1000,
               'task':'\\' * 200, 'alloc':'x'*200, 'stream':'stderr', '_time':'2026-10-31T04:00:00Z'}
        samples = contract.bounded_samples([row] * 10)
        self.assertEqual(len(samples),3)
        self.assertTrue(all(len(json.dumps(r,ensure_ascii=False).encode())<=900 for r in samples))
        engine = self.mod.Engine(lambda rule, now: {'status':'ok','value':3,'total':4,'sampledAt':now,'samples':samples,'sample':samples[0]['_msg']},lambda:1000)
        rules = [{'target':str(n),'logQuery':{'contains':'failed'}} for n in range(20)]
        engine.exchange(rules)
        for _ in rules: engine.step()
        self.assertLess(len(json.dumps(engine.exchange(rules),ensure_ascii=False).encode()),65536)

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
