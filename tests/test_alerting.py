import json
import io
import urllib.error
import os
import tempfile
import unittest
from unittest.mock import patch

from luma.control import alerting, database
from luma.control.alert_notifications import FeishuError, validate_credentials, send_feishu
from luma.control import alert_notifications as notifications
from luma.errors import LumaError


class AlertingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'LUMA_CONTROL_STATE_DIR': self.temp.name})
        self.env.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.env.stop)

    def channel(self):
        return alerting.save_channel({'name':'飞书','appId':'cli_test1234','appSecret':'private-sign','chatId':'oc_group1234'})

    def rule(self, **kw):
        return alerting.save_rule({'name':'磁盘','metric':'node.disk','threshold':90,'forSeconds':30,**kw},now=1000)

    def state(self, now, value=95):
        return {'clusterId':'test','nodes':{'node-a':{'agent':{'lastSeen':now,'metricsCollectedAt':now,'metrics':{'diskUsedPercent':value}}}}}

    def items(self,resource):
        return alerting.dispatch('GET',resource)['items']

    def test_duration_dedupe_recovery_and_order(self):
        ch=self.channel(); self.rule(channelIds=[ch['id']])
        alerting.tick(self.state(1000),now=1000)
        alerting.tick(self.state(1015),now=1015)
        self.assertEqual(self.items('incidents')[0]['status'],'pending')
        alerting.tick(self.state(1030),now=1030)
        alerting.tick(self.state(1045),now=1045)
        self.assertEqual(len(self.items('incidents')),1)
        self.assertEqual(len(self.items('deliveries')),1)
        sent=[]
        alerting.deliver_pending(now=1045,transport=lambda *args:sent.append(args))
        alerting.tick(self.state(1060,50),now=1060)
        self.assertEqual(self.items('incidents')[0]['status'],'resolved')
        self.assertEqual(len(self.items('deliveries')),2)
        alerting.deliver_pending(now=1060,transport=lambda *args:sent.append(args))
        self.assertIn('告警触发',sent[0][3]); self.assertIn('告警恢复',sent[1][3])

    def test_missing_data_does_not_recover_and_breaks_pending(self):
        self.rule()
        alerting.tick(self.state(1000),now=1000)
        alerting.tick(self.state(1015,None),now=1015)
        alerting.tick(self.state(1030),now=1030)
        self.assertEqual(self.items('incidents')[0]['status'],'pending')
        alerting.tick(self.state(1045),now=1045)
        alerting.tick(self.state(1060),now=1060)
        alerting.tick(self.state(1075,None),now=1075)
        self.assertEqual(self.items('incidents')[0]['status'],'firing')
        self.assertTrue(self.items('incidents')[0]['noData'])

    def test_recovery_cancels_undelivered_firing(self):
        ch=self.channel();self.rule(forSeconds=0,channelIds=[ch['id']])
        alerting.tick(self.state(1000),now=1000)
        with patch('luma.control.alerting.time.time',return_value=1001): alerting.dispatch('POST','silence',{'seconds':60})
        alerting.tick(self.state(1015,20),now=1015)
        sent=[]
        alerting.deliver_pending(now=1070,transport=lambda *a:sent.append(a))
        self.assertFalse(sent)
        self.assertEqual(self.items('deliveries')[0]['status'],'cancelled')

    def test_recovery_during_silence_deferred_if_firing_was_sent(self):
        ch=self.channel();self.rule(forSeconds=0,channelIds=[ch['id']])
        alerting.tick(self.state(1000),now=1000)
        sent=[]
        alerting.deliver_pending(now=1000,transport=lambda *a:sent.append(a))
        with patch('luma.control.alerting.time.time',return_value=1001): alerting.dispatch('POST','silence',{'seconds':60})
        alerting.tick(self.state(1015,20),now=1015)
        alerting.deliver_pending(now=1015,transport=lambda *a:sent.append(a))
        self.assertEqual(len(sent),1)
        alerting.deliver_pending(now=1061,transport=lambda *a:sent.append(a))
        self.assertEqual(len(sent),2)
        self.assertIn('告警恢复',sent[-1][3])

    def test_manager_gap_does_not_count_toward_duration(self):
        self.rule()
        alerting.tick(self.state(1000),now=1000)
        alerting.tick(self.state(1500),now=1500)
        self.assertEqual(self.items('incidents')[0]['status'],'pending')

    def test_nodata_alert_is_explicit_and_can_recover(self):
        self.rule(noData='alert',forSeconds=0)
        alerting.tick(self.state(1000,None),now=1000)
        self.assertTrue(self.items('incidents')[0]['noData'])
        alerting.tick(self.state(1015,50),now=1015)
        self.assertEqual(self.items('incidents')[0]['status'],'resolved')

    def test_silence_expiry_ack_and_reminder(self):
        ch=self.channel(); self.rule(forSeconds=0,repeatSeconds=60,silencedUntil=1050,channelIds=[ch['id']])
        alerting.tick(self.state(1000),now=1000)
        self.assertEqual(self.items('deliveries'),[])
        alerting.tick(self.state(1060),now=1060)
        self.assertEqual(len(self.items('deliveries')),1)
        incident=self.items('incidents')[0]
        alerting.dispatch('POST',f"incidents/{incident['id']}/ack")
        alerting.tick(self.state(1200),now=1200)
        self.assertEqual(len(self.items('deliveries')),1)
        alerting.deliver_pending(now=1200,transport=lambda *args:None)
        alerting.tick(self.state(1215,20),now=1215)
        self.assertEqual(len(self.items('deliveries')),2)

    def test_ack_cancels_queued_and_crashed_reminder(self):
        ch=self.channel();self.rule(forSeconds=0,repeatSeconds=60,channelIds=[ch['id']])
        alerting.tick(self.state(1000),now=1000)
        sent=[]
        alerting.deliver_pending(now=1000,transport=lambda *a:sent.append(a))
        alerting.tick(self.state(1060),now=1060)
        incident=self.items('incidents')[0]
        with database.transaction() as conn:
            conn.execute("UPDATE alert_outbox SET status='sending',lease_until=1070,lease_token='dead' WHERE kind='reminder'")
        alerting.dispatch('POST',f"incidents/{incident['id']}/ack")
        alerting.deliver_pending(now=1070,transport=lambda *a:sent.append(a))
        self.assertEqual(len(sent),1)
        self.assertEqual(self.items('deliveries')[0]['status'],'cancelled')

    def test_retry_persists_and_never_records_credentials(self):
        ch=self.channel()
        with patch('luma.control.alerting.time.time',return_value=1000):
            delivery=alerting.dispatch('POST',f"channels/{ch['id']}/test")['delivery']
        def fail(*args): raise ValueError('private-sign '+args[0])
        alerting.deliver_pending(now=1000,transport=fail)
        first=self.items('deliveries')[0]
        self.assertEqual(first['status'],'retry'); self.assertEqual(first['attempts'],1)
        self.assertNotIn('private-sign',json.dumps(first))
        calls=[]
        alerting.deliver_pending(now=1001,transport=lambda *args:calls.append(args))
        self.assertFalse(calls)
        alerting.deliver_pending(now=1015,transport=lambda *args:calls.append(args))
        self.assertEqual(self.items('deliveries')[0]['status'],'sent')
        self.assertEqual(len(calls),1)

    def test_expired_lease_does_not_send_closed_rule_alert(self):
        ch=self.channel();rule=self.rule(forSeconds=0,channelIds=[ch['id']])
        alerting.tick(self.state(1000),now=1000)
        with database.transaction() as conn: conn.execute("UPDATE alert_outbox SET status='sending',attempts=1,lease_until=1060,lease_token='dead'")
        alerting.save_rule({'id':rule['id'],'enabled':False},now=1015)
        sent=[]
        alerting.deliver_pending(now=1060,transport=lambda *a:sent.append(a))
        self.assertFalse(sent)
        self.assertEqual(self.items('deliveries')[0]['status'],'cancelled')

    def test_retention_protects_active_and_pending_revalidates(self):
        ch=self.channel();self.rule(forSeconds=0,channelIds=[ch['id']])
        alerting.tick(self.state(1000),now=1000)
        alerting.deliver_pending(now=1000,transport=lambda *a:None)
        with database.transaction() as conn:
            plan=alerting.retention_plan(conn,cutoff=2000)
            self.assertEqual(plan['incidentIds'],[])
            self.assertEqual(plan['deliveryIds'],[])
        alerting.tick(self.state(1015,10),now=1015)
        with database.transaction() as conn:
            plan=alerting.retention_plan(conn,cutoff=2000)
            self.assertEqual(plan['incidentIds'],[])  # recovery outstanding
        alerting.deliver_pending(now=1015,transport=lambda *a:None)
        with database.transaction() as conn:
            plan=alerting.retention_plan(conn,cutoff=2000)
            self.assertEqual(len(plan['incidentIds']),1)
            conn.execute("UPDATE alert_outbox SET status='retry' WHERE kind='resolved'")
            deleted=alerting.prune(conn,plan)
            self.assertEqual(deleted['incidentsDeleted'],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM alert_events').fetchone()[0],3)

    def test_appbot_test_and_rule_send_correct_group_and_retry_uuid(self):
        ch=self.channel();self.rule(forSeconds=0,channelIds=[ch['id']])
        with patch('luma.control.alerting.time.time',return_value=1000): alerting.dispatch('POST',f"channels/{ch['id']}/test")
        alerting.tick(self.state(1000),now=1000)
        calls=[]
        def first_fail(*args):
            calls.append(args)
            raise FeishuError('rate_limited')
        alerting.deliver_pending(now=1000,transport=first_fail,limit=1)
        alerting.deliver_pending(now=1015,transport=lambda *a:calls.append(a))
        self.assertEqual(len(calls),3)
        self.assertTrue(all(a[:3]==('cli_test1234','private-sign','oc_group1234') for a in calls))
        self.assertEqual(calls[0][4],calls[1][4])
        self.assertNotEqual(calls[1][4],calls[2][4])
        self.assertTrue(all(len(a[4])<=50 for a in calls))
        self.assertNotIn('private-sign',json.dumps(self.items('deliveries')))

    def test_permanent_app_permission_failure_stops_retry(self):
        ch=self.channel()
        with patch('luma.control.alerting.time.time',return_value=1000): alerting.dispatch('POST',f"channels/{ch['id']}/test")
        def fail(*args): raise FeishuError('no_permission',retryable=False)
        alerting.deliver_pending(now=1000,transport=fail)
        delivery=self.items('deliveries')[0]
        self.assertEqual(delivery['status'],'failed')
        self.assertEqual(delivery['attempts'],1)
        self.assertIn('im:message:send_as_bot',delivery['lastError'])

    def test_crashed_lease_reclaimed_only_after_expiry(self):
        ch=self.channel()
        with patch('luma.control.alerting.time.time',return_value=1000): alerting.dispatch('POST',f"channels/{ch['id']}/test")
        with database.transaction() as conn: conn.execute("UPDATE alert_outbox SET status='sending',attempts=1,lease_until=1060,lease_token='dead'")
        calls=[]
        alerting.deliver_pending(now=1059,transport=lambda *a:calls.append(a)); self.assertFalse(calls)
        alerting.deliver_pending(now=1060,transport=lambda *a:calls.append(a)); self.assertEqual(len(calls),1)

    def test_secret_write_only_and_preserved_update(self):
        ch=self.channel()
        self.assertNotIn('12345678',json.dumps(ch));self.assertNotIn('private-sign',json.dumps(ch))
        updated=alerting.save_channel({'id':ch['id'],'name':'改名'})
        self.assertTrue(updated['appSecretConfigured'])
        with self.assertRaises(LumaError): alerting.save_channel({'id':ch['id'],'appSecret':''})

    def test_rule_edit_cancels_active_outbox(self):
        ch=self.channel(); rule=self.rule(forSeconds=0,channelIds=[ch['id']])
        alerting.tick(self.state(1000),now=1000)
        alerting.save_rule({'id':rule['id'],'threshold':99},now=1015)
        self.assertEqual(self.items('incidents')[0]['status'],'closed')
        self.assertEqual(self.items('deliveries')[0]['status'],'cancelled')
        self.assertEqual(self.items('rules')[0]['channelIds'],[ch['id']])

    def test_build_group_uses_real_request_and_pending_is_not_recovery(self):
        alerting.save_rule({'name':'构建','metric':'build.failed','threshold':0,'forSeconds':0},now=1000)
        state={'buildRuns':{'a':{'id':'a','request':{'name':'web'},'createdAt':1000,'status':'failed'}}}
        alerting.tick(state,now=1000)
        self.assertEqual(self.items('incidents')[0]['target'],'web')
        state['buildRuns']['b']={'id':'b','request':{'name':'web'},'createdAt':1010,'status':'running'}
        alerting.tick(state,now=1015)
        self.assertEqual(self.items('incidents')[0]['status'],'firing')
        state['buildRuns']['b']['status']='succeeded'
        alerting.tick(state,now=1030)
        self.assertEqual(self.items('incidents')[0]['status'],'resolved')

    def test_global_silence_defers_existing_queue(self):
        ch=self.channel();self.rule(forSeconds=0,channelIds=[ch['id']])
        alerting.tick(self.state(1000),now=1000)
        with patch('luma.control.alerting.time.time',return_value=1001):
            alerting.dispatch('POST','silence',{'seconds':60})
        calls=[]
        alerting.deliver_pending(now=1002,transport=lambda *a:calls.append(a))
        self.assertFalse(calls)
        alerting.deliver_pending(now=1061,transport=lambda *a:calls.append(a))
        self.assertEqual(len(calls),1)

    def test_retry_attempt_budget_persists(self):
        ch=self.channel()
        with patch('luma.control.alerting.time.time',return_value=1000):
            alerting.dispatch('POST',f"channels/{ch['id']}/test")
        calls=[]
        def fail(*args):
            calls.append(args)
            raise ValueError('error')
        now=1000
        for _ in range(12):
            alerting.deliver_pending(now=now,transport=fail)
            now+=4000
        self.assertEqual(len(calls),8)
        self.assertEqual(self.items('deliveries')[0]['status'],'failed')

    def test_sql_snapshot_does_not_load_events_or_all_build_history(self):
        from luma.control.state import save_state
        state=self.state(1000)
        state['buildRuns']={str(i):{'id':str(i),'request':{'name':'web'},'status':'failed' if i<19 else 'succeeded','createdAt':i+1,'events':[{'message':'huge event'}]} for i in range(20)}
        state['builderTasks']={'x':{'createdAt':900,'status':'queued','progress':[{'message':'huge'}]}}
        save_state(state)
        snapshot=alerting.load_evaluation_state(now=1000)
        self.assertEqual(len(snapshot['buildRuns']),1)
        self.assertNotIn('events',next(iter(snapshot['buildRuns'].values())))
        self.assertEqual(snapshot['_alertQueueAge']['builder'],100)
        self.assertEqual(snapshot['nodes']['node-a'],state['nodes']['node-a'])

    def test_pagination_and_validation(self):
        self.rule(forSeconds=0)
        for now,value in [(1000,95),(1015,10),(1030,95),(1045,10)]: alerting.tick(self.state(now,value),now=now)
        first=alerting.dispatch('GET','incidents',query={'limit':1})
        second=alerting.dispatch('GET','incidents',query={'limit':1,'cursor':first['nextCursor']})
        self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        with self.assertRaises(LumaError): alerting.dispatch('GET','incidents',query={'cursor':'oops'})
        with self.assertRaises(LumaError): self.rule(threshold=float('nan'))


class FeishuTransportTest(unittest.TestCase):
    def setUp(self):
        notifications._TOKEN_CACHE.clear()

    def send(self, text='test', delivery='stable-delivery-id'):
        return send_feishu('cli_test1234','private-app-secret','oc_group1234',text,delivery)

    def test_credentials_validation(self):
        for app,secret,chat in [('https://evil.test','secret','oc_group1234'),('cli_test1234','secret','https://evil.test'),('cli_test1234','has spaces','oc_group1234'),('cli_test1234','','oc_group1234')]:
            with self.assertRaises(LumaError): validate_credentials(app,secret,chat)

    def test_token_reused_and_message_targets_chat_with_stable_uuid(self):
        calls=[]
        def post(url,body,**kwargs):
            calls.append((url,body,kwargs))
            return {'code':0,'tenant_access_token':'tenant-secret-token','expire':7200} if url==notifications.AUTH_URL else {'code':0}
        with patch.object(notifications,'_post',side_effect=post):
            self.send('hello','same-id');self.send('hello','same-id')
        self.assertEqual(sum(c[0]==notifications.AUTH_URL for c in calls),1)
        sends=[c for c in calls if c[0]==notifications.MESSAGE_URL]
        self.assertEqual(sends[0][1],{'receive_id':'oc_group1234','msg_type':'text','content':'{"text": "hello"}','uuid':'same-id'})
        self.assertEqual(sends[0][2]['token'],'tenant-secret-token')
        self.assertEqual(sends[0][1]['uuid'],sends[1][1]['uuid'])

    def test_token_expiry_and_secret_rotation_reacquire(self):
        with patch.object(notifications,'_post',return_value={'code':0,'tenant_access_token':'tenant-token','expire':100}) as post:
            with patch.object(notifications.time,'monotonic',return_value=1000): self.send()
            with patch.object(notifications.time,'monotonic',return_value=1050): self.send()
            with patch.object(notifications.time,'monotonic',return_value=1100): self.send()
            send_feishu('cli_test1234','rotated-secret','oc_group1234','text','delivery')
        auths=[c for c in post.call_args_list if c.args[0]==notifications.AUTH_URL]
        self.assertEqual(len(auths),3)

    def test_invalid_cached_token_reacquires_once(self):
        calls=[]
        def post(url,body,**kwargs):
            calls.append(url)
            if url==notifications.AUTH_URL: return {'code':0,'tenant_access_token':'token-'+str(len(calls)),'expire':7200}
            if calls.count(notifications.MESSAGE_URL)==1: raise FeishuError('invalid_token')
            return {'code':0}
        with patch.object(notifications,'_post',side_effect=post): self.send()
        self.assertEqual(calls,[notifications.AUTH_URL,notifications.MESSAGE_URL,notifications.AUTH_URL,notifications.MESSAGE_URL])

    def test_error_categories_do_not_echo_provider_secrets(self):
        for body,auth,category,retryable in [
            ({'code':10014,'msg':'private-secret invalid'},True,'invalid_credentials',False),
            ({'code':99991672,'msg':'secret permission denied'},False,'no_permission',False),
            ({'code':230013,'msg':'Bot is not in chat secret'},False,'bot_not_in_chat',False),
            ({'code':99991400,'msg':'secret'},False,'rate_limited',True),
        ]:
            error=notifications._classify(body,auth=auth)
            self.assertEqual(error.category,category)
            self.assertEqual(error.retryable,retryable)
            self.assertNotIn('private-secret',str(error))

    def test_http_400_business_codes_are_classified_and_response_closed(self):
        for code,msg,category in [(99991663,'Invalid access token for authorization secret-token','invalid_token'),(99991400,'rate limited private-secret','rate_limited')]:
            stream=io.BytesIO(json.dumps({'code':code,'msg':msg}).encode())
            error=urllib.error.HTTPError(notifications.MESSAGE_URL,400,'Bad Request',{},stream)
            with patch('urllib.request.build_opener') as build:
                build.return_value.open.side_effect=error
                with self.assertRaises(FeishuError) as result:
                    notifications._post(notifications.MESSAGE_URL,{},token='private-token',deadline=notifications.time.monotonic()+16)
            self.assertEqual(result.exception.category,category)
            self.assertTrue(result.exception.retryable)
            self.assertTrue(stream.closed)
            self.assertNotIn('secret-token',str(result.exception))
            self.assertNotIn('private-secret',str(result.exception))

    def test_http_400_expired_token_reacquires_and_retries_real_transport(self):
        def response(data):
            from unittest.mock import MagicMock
            result=MagicMock()
            result.__enter__.return_value.read.return_value=json.dumps(data).encode()
            return result
        failed_stream=io.BytesIO(b'{"code":99991663,"msg":"Invalid access token for authorization"}')
        failure=urllib.error.HTTPError(notifications.MESSAGE_URL,400,'Bad Request',{},failed_stream)
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.side_effect=[response({'code':0,'tenant_access_token':'old-token','expire':7200}),failure,response({'code':0,'tenant_access_token':'new-token','expire':7200}),response({'code':0})]
            self.send()
        calls=build.return_value.open.call_args_list
        self.assertEqual(len(calls),4)
        self.assertEqual(calls[1].args[0].headers['Authorization'],'Bearer old-token')
        self.assertEqual(calls[3].args[0].headers['Authorization'],'Bearer new-token')
        self.assertTrue(failed_stream.closed)

    def test_http_transport_uses_fixed_url_no_redirect_and_bounded_timeout(self):
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value=b'{"code":0}'
            notifications._post(notifications.MESSAGE_URL,{},token='sensitive-token',deadline=notifications.time.monotonic()+16)
            request=build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,notifications.MESSAGE_URL)
            self.assertEqual(request.headers['Authorization'],'Bearer sensitive-token')
            self.assertLessEqual(build.return_value.open.call_args.kwargs['timeout'],8)
            self.assertTrue(any(isinstance(x,notifications._NoRedirect) for x in build.call_args.args))
        with self.assertRaises(FeishuError): notifications._post('https://localhost/',{},deadline=99999999999)
