"""Authentication, scope isolation and read budgets for task investigations."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from luma.control import observe_tasks
from luma.errors import LumaError

try:
    from luma_observe.query import ObserveClient
except ImportError:
    ObserveClient = None


class TaskQueryTests(unittest.TestCase):
    def test_filters_cannot_escape_application_or_query_syntax(self):
        query = observe_tasks.task_query({"app": "orders-prod", "task_name": 'job" } || { true',
                                          "attribute": "order.id", "value": '42" || true', "min_duration_ms": "250"})
        rendered = observe_tasks.trace_query(query)
        self.assertIn('resource.luma.stack = "orders-prod"', rendered)
        self.assertIn('span.task.name = ' + json.dumps(query["task_name"]), rendered)
        self.assertIn('span.order.id = ' + json.dumps(query["value"]), rendered)
        self.assertIn('duration >= 250ms', rendered)
        for params in ({"app": 'orders" || true'}, {"app": "orders", "hours": 73},
                       {"app": "orders", "offset": 2001}, {"app": "orders", "trace_id": "../other"},
                       {"app": "orders", "attribute": "a) || true", "value": "x"},
                       {"app": "orders", "attribute": "order.id"}):
            with self.subTest(params=params), self.assertRaises(LumaError):
                observe_tasks.task_query(params)

    def test_scope_and_activity_guards_survive_every_metric_query(self):
        queries = observe_tasks.metric_queries("orders-prod", 6)
        self.assertTrue(all('luma_stack="orders-prod"' in expression for expression in queries.values()))
        for name in ("oldest", "no_progress"):
            self.assertIn("task_active_tracking_overflow", queries[name])
            self.assertIn("task_active{", queries[name])
            self.assertIn("worker_heartbeat_seconds", queries[name])


class TaskHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_precedes_reads_and_validation(self):
        with patch.object(observe_tasks, "_authorize", side_effect=LumaError("unauthorized")), patch.object(observe_tasks, "_read", new_callable=AsyncMock) as read:
            with self.assertRaisesRegex(LumaError, "unauthorized"):
                await observe_tasks.handle_task_observe("bad", "trace", {})
            read.assert_not_awaited()

    async def test_admission_and_cancellation_release_capacity(self):
        slots = asyncio.Semaphore(1)
        started = asyncio.Event()

        async def wait(*_args):
            started.set()
            await asyncio.Event().wait()

        with patch.object(observe_tasks, "_authorize"), patch.object(observe_tasks, "READ_SLOTS", slots), patch.object(observe_tasks, "_read", side_effect=wait):
            first = asyncio.create_task(observe_tasks.handle_task_observe("token", "events", {"app": "orders"}))
            await started.wait()
            second = await observe_tasks.handle_task_observe("token", "events", {"app": "orders"})
            self.assertEqual(second["state"], "rate_limited")
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertFalse(slots.locked())

    @unittest.skipUnless(ObserveClient, "Control query extra requires Python 3.10+")
    async def test_trace_detail_uses_sdk_to_exclude_other_applications(self):
        trace_id = "a" * 32
        def batch(app, span_id):
            return {"resource": {"attributes": [{"key": "luma.stack", "value": {"stringValue": app}}]},
                    "scopeSpans": [{"spans": [{"traceId": trace_id, "spanId": span_id, "name": app,
                                              "startTimeUnixNano": "1000000000", "endTimeUnixNano": "2000000000"}]}]}
        body = {"resourceSpans": [batch("orders", "1" * 16), batch("another-app", "2" * 16)]}
        with patch.object(ObserveClient, "request", AsyncMock(return_value=json.dumps(body).encode())):
            result = await observe_tasks._read("trace", observe_tasks.task_query({"app": "orders", "trace_id": trace_id}))
        self.assertEqual([span["name"] for span in result["spans"]], ["orders"])
        self.assertTrue(result["partial"])

    @unittest.skipUnless(ObserveClient, "Control query extra requires Python 3.10+")
    async def test_trace_search_caps_results_and_propagates_source_failures(self):
        from luma_observe.query import QueryUnavailable
        traces = [{"traceID": f"{i:032x}", "durationMs": 12} for i in range(51)]
        query = observe_tasks.task_query({"app": "orders", "hours": 6})
        with patch.object(ObserveClient, "request", AsyncMock(return_value=json.dumps({"traces": traces}).encode())) as read:
            result = await observe_tasks._read("traces", query)
        self.assertEqual(len(result["items"]), 50)
        self.assertTrue(result["possibly_truncated"])
        self.assertEqual(read.call_args.args[2]["end"] - read.call_args.args[2]["start"], 6 * 3600)
        with patch.object(ObserveClient, "request", AsyncMock(side_effect=QueryUnavailable("timeout"))):
            result = await observe_tasks._read("traces", query)
        self.assertEqual(result["state"], "timeout")


class TaskRouteTests(unittest.TestCase):
    def test_dashboard_route_requires_authentication_and_dispatches_typed_reads(self):
        from starlette.testclient import TestClient
        from luma.control.server import create_app
        from luma.control.state import init_state
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": directory}):
            state = init_state(domain="luma.example.test", cluster_id="task-test")
            with TestClient(create_app()) as client:
                response = client.get("/v1/dashboard/observe/tasks/events?app=orders")
                self.assertEqual(response.status_code, 401)
                with patch.object(observe_tasks, "_read", AsyncMock(return_value={"state": "ok", "items": []})) as read:
                    response = client.get("/v1/dashboard/observe/tasks/events?app=orders&hours=6", headers={"Authorization": "Bearer " + state["deployToken"]})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(read.call_args.args[0], "events")
                    self.assertEqual(read.call_args.args[1]["app"], "orders")
                    self.assertEqual(read.call_args.args[1]["hours"], 6)
