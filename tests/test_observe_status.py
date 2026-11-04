import json
import unittest
from unittest.mock import patch

from luma.control import observe_status, observe_tasks
from luma import agent
from luma.errors import LumaError


class ObserveSummaryTests(unittest.TestCase):
    def test_empty_zero_and_failure_are_distinct(self):
        for body, expected in [({"status":"success","data":{"result":[]}}, "empty"),
                               ({"status":"success","data":{"result":[{"metric":{},"value":[100,"0"]}]}}, "ok")]:
            with patch.object(observe_status, "read_json", return_value={"state":"ok","body":body}):
                result = observe_status.metric("up", 100)
                self.assertEqual(result["state"], expected)
                self.assertEqual(result["value"], 0 if expected == "ok" else None)
        with patch.object(observe_status, "read_json", return_value={"state":"timeout"}):
            self.assertEqual(observe_status.metric("up",100), {"state":"timeout"})

    def test_status_requires_auth_before_upstream_reads(self):
        with patch.object(observe_status, "load_auth_state", return_value={}), patch.object(observe_status, "require_token", side_effect=LumaError("unauthorized")), patch.object(observe_status, "read_json") as read:
            with self.assertRaises(LumaError):
                observe_status.handle_observe_status("invalid", {})
            with self.assertRaises(LumaError):
                observe_status.handle_external_alerts("invalid")
            read.assert_not_called()

    def test_invalid_upstream_is_not_an_empty_success(self):
        with patch.object(observe_status, "read_json", return_value={"state": "ok", "body": {"status": "success", "data": None}}):
            self.assertEqual(observe_status.metric("up", 100)["state"], "invalid_response")
        with patch.object(observe_status, "load_auth_state", return_value={}), patch.object(observe_status, "require_token"), patch.object(observe_status, "read_json", return_value={"state": "ok", "body": {"error": "unavailable"}}):
            self.assertEqual(observe_status.handle_external_alerts("token")["state"], "invalid_response")

    def test_scope_time_and_query_escaping(self):
        q = observe_tasks.task_query({"app":"orders", "since":100, "until":200, "service":'a" || true', "environment":"production"})
        expr = observe_tasks.trace_query(q)
        self.assertIn('resource.service.name = "a\\" || true"',expr)
        metrics = observe_tasks.metric_queries("orders",1,q)
        self.assertIn('[100s]',metrics['executions'])
        self.assertIn('deployment_environment_name="production"',metrics['active'])
        with self.assertRaises(LumaError):
            observe_tasks.task_query({"app":"orders","since":200,"until":100})

    def test_missing_optional_labels_use_verified_runtime_identity(self):
        agent._CONTAINER_IDENTITIES.clear()
        containers = {'abc123':{'service':'nomad:alloc','nomadAllocId':'alloc'}}
        inspect = {'Id':'abc123','Name':'worker-alloc','Config':{'Env':['NOMAD_ALLOC_ID=alloc','NOMAD_JOB_ID=orders','NOMAD_TASK_NAME=worker','SECRET=do-not-export']}}
        with patch.object(agent.subprocess,'run') as run:
            run.return_value.returncode=0;run.return_value.stdout=json.dumps(inspect)
            agent._complete_container_identities('docker',containers)
        self.assertEqual(containers['abc123']['service'],'orders')
        self.assertEqual(containers['abc123']['nomadTask'],'worker')
        self.assertNotIn('SECRET',json.dumps(containers))
        agent._CONTAINER_IDENTITIES.clear()
        inspect['Config']['Env'][0]='NOMAD_ALLOC_ID=wrong'
        original = {'abc123':{'service':'nomad:alloc','nomadAllocId':'alloc'}}
        with patch.object(agent.subprocess,'run') as run:
            run.return_value.returncode=0;run.return_value.stdout=json.dumps(inspect)
            agent._complete_container_identities('docker',original)
        self.assertEqual(original['abc123']['service'],'nomad:alloc')

    def test_infrastructure_does_not_inflate_service_totals(self):
        from luma.control.server import _service_stats_by_name
        rows=[{'name':'node','containerStats':[{'containerId':'a','service':'orders','containerRole':'infrastructure','memoryUsageBytes':100}]}]
        with patch('luma.control.server._normalize_container_stats_for_engine',side_effect=lambda rows,**kw:rows):
            self.assertEqual(_service_stats_by_name(rows,compose_stacks=set()),{})

class ObserveCliTests(unittest.TestCase):
    def test_cli_passes_fixed_time_and_scope_to_shared_api(self):
        import io
        from contextlib import redirect_stdout
        from luma.cli import build_parser
        from luma.cli.observe import cmd_observe
        args=build_parser().parse_args(['observe','tasks','orders','--view','traces','--since','100','--until','200','--service','worker','--format','json'])
        with patch('luma.cli.observe.common._control_context',return_value=('https://luma.example','token',False,None)), patch('luma.cli.observe.common.ControlClient') as client, redirect_stdout(io.StringIO()) as out:
            client.return_value.request.return_value={'state':'ok','items':[]}
            self.assertEqual(cmd_observe(args),0)
            url=client.return_value.request.call_args.args[1]
            self.assertIn('/observe/tasks/traces?',url)
            self.assertIn('since=100',url)
            self.assertIn('service=worker',url)
            self.assertEqual(json.loads(out.getvalue())['result']['range'],{'since':100,'until':200})

    def test_cli_external_failure_preserves_control_evidence(self):
        import io
        from contextlib import redirect_stdout
        from luma.cli import build_parser
        from luma.cli.observe import cmd_observe
        args=build_parser().parse_args(['observe','alerts','--app','orders','--format','json'])
        with patch('luma.cli.observe.common._control_context',return_value=('https://luma.example','token',False,None)), patch('luma.cli.observe.common.ControlClient') as client, redirect_stdout(io.StringIO()) as out:
            client.return_value.request.side_effect=[{'items':[{'id':1}]},LumaError('unavailable')]
            cmd_observe(args)
            result=json.loads(out.getvalue())['result']
            self.assertEqual(len(result['control']['items']),1)
            self.assertEqual(result['alertmanager']['state'],'unreachable')
