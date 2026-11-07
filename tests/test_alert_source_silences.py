import base64
import copy
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from luma.alert_sources import source
from luma.control import alerting, database, feishu_callbacks


class SourceSilenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'LUMA_CONTROL_STATE_DIR': self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.channel = alerting.save_channel({'name': 'Operations', 'appId': 'cli_test1234', 'appSecret': 'app-private',
            'chatId': 'oc_group1234', 'cardActionsEnabled': True,
            'callbackVerificationToken': 'callback-private-token', 'callbackEncryptKey': 'callback-private-key'})
        self.rule = alerting.save_rule({'name': 'Task review', 'metric': 'app.log_matches', 'target': 'example-app',
            'threshold': 0, 'forSeconds': 0, 'repeatSeconds': 86400, 'logQuery': {'contains': 'needs_review'},
            'channelIds': [self.channel['id']]}, now=1000)
        self.alpha, self.beta = source('task', 'jobs.alpha'), source('task', 'jobs.beta')
        self.sent = []

    def tick(self, now, counts, *, unknown=0):
        sources = [{**item, 'count': count} for item, count in counts]
        samples = [{'task': 'worker', 'task_name': item['name'], '_msg': item['name'] + ': needs_review'} for item, count in counts]
        sample = {'status': 'ok', 'value': sum(count for _, count in counts) + unknown, 'total': 50,
                  'sources': sources, 'samples': samples, 'sampledAt': now}
        alerting.tick({'domain': 'luma.test', '_logAlerts': {self.rule['id']: sample}}, now=now)
        alerting.deliver_pending(now=now, transport=lambda *args: self.sent.append(json.loads(args[3])))
        return alerting.dispatch('GET', 'incidents')['items'][0]

    def silence(self, incident, item, now, seconds=3600):
        with patch.object(alerting.time, 'time', return_value=now):
            return alerting.dispatch('POST', f"incidents/{incident['id']}/silence-source", {'sourceKey': item['key'], 'seconds': seconds})

    def test_source_mute_retains_incident_and_new_source_notifies_before_repeat_interval(self):
        incident = self.tick(1000, [(self.alpha, 2)])
        self.silence(incident, self.alpha, 1001)
        quiet = self.tick(1060, [(self.alpha, 2)])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(quiet['status'], 'firing')
        self.assertEqual(quiet['value'], 2)
        self.assertIsNone(quiet['acknowledgedAt'])
        self.tick(1075, [(self.alpha, 2), (self.beta, 1)])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[-1]['kind'], 'firing')
        self.assertEqual([item['name'] for item in self.sent[-1]['sources']], ['jobs.beta'])
        self.assertNotIn('jobs.alpha', json.dumps(self.sent[-1]['card']))
        self.tick(4700, [(self.alpha, 2), (self.beta, 1)])
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(len(self.sent[-1]['sources']), 2)

    def test_recovery_is_a_separate_card_and_mute_survives_a_new_incident(self):
        incident = self.tick(1000, [(self.alpha, 1)])
        self.silence(incident, self.alpha, 1001)
        recovered = self.tick(1060, [])
        self.assertEqual(recovered['status'], 'resolved')
        self.assertEqual([card['kind'] for card in self.sent], ['firing', 'resolved'])
        self.assertNotIn('sources', self.sent[-1])
        fresh = self.tick(1075, [(self.alpha, 1)])
        self.assertNotEqual(fresh['id'], incident['id'])
        self.assertEqual(len(self.sent), 2)
        self.tick(1090, [(self.alpha, 1), (self.beta, 1)])
        self.assertEqual(len(self.sent), 3)

    def test_unknown_sources_remain_visible_and_revocation_resumes_immediately(self):
        incident = self.tick(1000, [(self.alpha, 1)])
        self.silence(incident, self.alpha, 1001)
        self.tick(1060, [(self.alpha, 1)], unknown=1)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[-1]['sources'], [])
        with patch.object(alerting.time, 'time', return_value=1070):
            alerting.dispatch('POST', 'source-silences/remove', {'ruleId': self.rule['id'], 'target': 'example-app', 'sourceKey': self.alpha['key']})
        self.tick(1075, [(self.alpha, 1)])
        self.assertEqual(len(self.sent), 3)

    def test_same_task_in_another_application_or_rule_keeps_notifying(self):
        incident = self.tick(1000, [(self.alpha, 1)])
        self.silence(incident, self.alpha, 1001)
        for target in ('other-app', 'example-app'):
            other = alerting.save_rule({**self.rule, 'id': None, 'target': target}, now=1010)
            sample = {'status': 'ok', 'value': 1, 'total': 10, 'sampledAt': 1015,
                      'sources': [{**self.alpha, 'count': 1}], 'samples': []}
            alerting.tick({'_logAlerts': {other['id']: sample}}, now=1015)
            alerting.deliver_pending(now=1015, transport=lambda *args: self.sent.append(json.loads(args[3])))
        self.assertEqual(len(self.sent), 3)

    def test_queued_mixed_source_card_is_rebuilt_and_rule_change_clears_matchers(self):
        with patch.object(alerting, 'deliver_pending'):
            incident = self.tick(1000, [(self.alpha, 1), (self.beta, 1)])
        self.silence(incident, self.alpha, 1001)
        self.tick(1015, [(self.alpha, 1), (self.beta, 1)])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual([item['name'] for item in self.sent[0]['sources']], ['jobs.beta'])
        alerting.save_rule({**self.rule, 'name': 'Renamed task review'}, now=1018)
        with patch.object(alerting.time, 'time', return_value=1018):
            self.assertEqual(len(alerting.dispatch('GET', 'source-silences')['items']), 1)
        alerting.save_rule({**self.rule, 'logQuery': {'contains': 'other_failure'}}, now=1020)
        with patch.object(alerting.time, 'time', return_value=1020):
            self.assertEqual(alerting.dispatch('GET', 'source-silences')['items'], [])

    def action_event(self, *, event_id='callback-1'):
        action = next(action['value'] for element in self.sent[0]['card']['elements'] if element['tag'] == 'action'
                      for action in element['actions'] if action.get('value', {}).get('action') == 'silence_source')
        return {'schema': '2.0', 'header': {'event_id': event_id, 'event_type': 'card.action.trigger', 'app_id': 'cli_test1234', 'token': 'callback-private-token'},
                'event': {'host': 'im_message', 'operator': {'open_id': 'ou_operator'},
                          'context': {'open_chat_id': 'oc_group1234', 'open_message_id': 'om_message'},
                          'action': {'tag': 'button', 'value': action}}}

    def encode(self, event, now=1001, *, encrypt=True):
        raw = json.dumps(event).encode()
        if encrypt:
            padder = padding.PKCS7(128).padder()
            padded = padder.update(raw) + padder.finalize()
            iv = b'0123456789abcdef'
            cipher = Cipher(algorithms.AES(hashlib.sha256(b'callback-private-key').digest()), modes.CBC(iv)).encryptor()
            raw = json.dumps({'encrypt': base64.b64encode(iv + cipher.update(padded) + cipher.finalize()).decode()}).encode()
        headers = {'x-lark-request-timestamp': str(now), 'x-lark-request-nonce': 'event-nonce'}
        headers['x-lark-signature'] = hashlib.sha256((str(now) + 'event-nonce' + 'callback-private-key').encode() + raw).hexdigest()
        return raw, headers

    def test_signed_encrypted_callback_is_idempotent_and_updates_only_clicked_card(self):
        self.tick(1000, [(self.alpha, 1), (self.beta, 1)])
        raw, headers = self.encode(self.action_event())
        result = feishu_callbacks.handle('cli_test1234', raw, headers, now=1001)
        repeated = feishu_callbacks.handle('cli_test1234', raw, headers, now=1020)
        self.assertEqual(result, repeated)
        self.assertEqual(result['card']['data']['header'], self.sent[0]['card']['header'])
        self.assertIn('此来源已静默至', json.dumps(result['card'], ensure_ascii=False))
        with patch.object(alerting.time, 'time', return_value=1020):
            mute = alerting.dispatch('GET', 'source-silences')['items'][0]
            self.assertEqual(mute['until'], 4601)
        with database.transaction(immediate=False) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM alert_events WHERE kind='source_silenced'").fetchone()[0], 1)
        self.tick(1030, [(self.alpha, 1), (self.beta, 1)])
        self.assertEqual(len(self.sent), 1)  # Beta was already delivered on the original card.

    def test_callback_rejects_tampering_wrong_chat_wrong_source_and_expired_signature(self):
        self.tick(1000, [(self.alpha, 1)])
        raw, headers = self.encode(self.action_event())
        for app, data, signed, now in [('cli_other1234', raw, headers, 1001),
                                      ('cli_test1234', raw + b' ', headers, 1001),
                                      ('cli_test1234', raw, headers, 1400)]:
            with self.assertRaises(feishu_callbacks.CallbackDenied):
                feishu_callbacks.handle(app, data, signed, now=now)
        for changed in ('chat', 'source', 'token'):
            event = copy.deepcopy(self.action_event())
            if changed == 'chat':
                event['event']['context']['open_chat_id'] = 'oc_other1234'
            else:
                event['event']['action']['value']['sourceKey' if changed == 'source' else 'token'] = 'forged'
            raw, headers = self.encode(event)
            with self.assertRaises(feishu_callbacks.CallbackDenied):
                feishu_callbacks.handle('cli_test1234', raw, headers, now=1001)
        with patch.object(alerting.time, 'time', return_value=1001):
            self.assertEqual(alerting.dispatch('GET', 'source-silences')['items'], [])

    def test_callback_http_challenge_credentials_shared_and_not_exposed(self):
        second = alerting.save_channel({'name': 'Other group', 'appId': 'cli_test1234', 'appSecret': 'app-private', 'chatId': 'oc_other1234'})
        self.assertTrue(second['cardActionsEnabled'])
        public = json.dumps(alerting.dispatch('GET', 'channels'))
        self.assertNotIn('callback-private', public)
        challenge = {'type': 'url_verification', 'token': 'callback-private-token', 'challenge': 'challenge-value'}
        raw, _ = self.encode(challenge)
        app = Starlette(routes=[Route('/callback/{app_id}', feishu_callbacks.endpoint, methods=['POST'])])
        with TestClient(app) as client:
            response = client.post('/callback/cli_test1234', content=raw)
            self.assertEqual(response.json(), {'challenge': 'challenge-value'})
            self.assertEqual(client.post('/callback/cli_test1234', content=b'[]').status_code, 401)
            self.assertEqual(client.post('/callback/cli_test1234', content=b'x' * 65537).status_code, 413)
        alerting.save_channel({'id': self.channel['id'], 'cardActionsEnabled': False})
        self.assertTrue(all(not item['cardActionsEnabled'] for item in alerting.dispatch('GET', 'channels')['items']))


if __name__ == '__main__':
    unittest.main()
