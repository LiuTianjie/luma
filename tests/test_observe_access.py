"""验证观测读取与 OTLP 写入的配置边界及安全关闭状态。"""
import json
from pathlib import Path
import re
import urllib.parse
import runpy
import unittest

ROOT = Path(__file__).resolve().parents[1]
auth_config = runpy.run_path(str(ROOT / "observe/access_config.py"))["auth_config"]


class ObserveAccessTests(unittest.TestCase):
    def test_empty_credentials_leave_no_authorized_routes(self):
        self.assertEqual(auth_config({}), {"users": []})

    def test_roles_enable_independently(self):
        for variable, role in (("LUMA_OBSERVE_READ_TOKEN", "observe-reader"),
                               ("LUMA_OBSERVE_INGEST_TOKEN", "observe-ingester")):
            with self.subTest(role=role):
                users = auth_config({variable: "r" * 40})["users"]
                self.assertEqual([user["name"] for user in users], [role])
                self.assertEqual(users[0]["headers"], ["Authorization:"])
                self.assertNotIn("unauthorized_user", auth_config({variable: "r" * 40}))

    def test_read_routes_exclude_ingestion_admin_and_internal_apis(self):
        """读取身份仅匹配标准查询路径，管理和写入路径由网关拒绝。"""
        user = auth_config({"LUMA_OBSERVE_READ_TOKEN": "r" * 40})["users"][0]
        patterns = [path for route in user["url_map"] for path in route["src_paths"]]
        for path in ("/metrics/api/v1/query", "/metrics/api/v1/query_range", "/logs/select/logsql/query",
                     "/traces/api/search", "/traces/api/traces/" + "a" * 32):
            self.assertTrue(any(re.fullmatch(pattern, path) for pattern in patterns), path)
        for path in ("/metrics/api/v1/write", "/metrics/api/v1/admin/tsdb/delete_series",
                     "/logs/insert/jsonline", "/traces/flush", "/otlp/v1/traces",
                     "/metrics", "/debug/pprof", "/-/reload", "/traces/api/traces/../../flush"):
            self.assertFalse(any(re.fullmatch(pattern, path) for pattern in patterns), path)
        self.assertNotIn("default_url", user)

    def test_ingester_only_routes_standard_otlp_metrics_and_traces(self):
        user = auth_config({"LUMA_OBSERVE_INGEST_TOKEN": "w" * 40})["users"][0]
        self.assertEqual(user["url_map"], [{"src_paths": ["/otlp/v1/traces", "/otlp/v1/metrics"],
                                           "url_prefix": "http://127.0.0.1:4318", "drop_src_path_prefix_parts": 1}])

    def test_invalid_or_shared_credentials_fail_without_logging_values(self):
        """无效格式与读写共用凭证给出固定错误，错误信息仅包含字段名。"""
        cases = [("short-token", ""), ("r" * 40 + "\n", ""), ("r" * 40, "r" * 40)]
        for reader, writer in cases:
            with self.subTest(length=len(reader)):
                with self.assertRaises(ValueError) as result:
                    auth_config({"LUMA_OBSERVE_READ_TOKEN": reader, "LUMA_OBSERVE_INGEST_TOKEN": writer})
                self.assertNotIn(reader, str(result.exception))

    def test_scoped_reader_enforces_namespace_apps_and_trace_lookup_by_id(self):
        """按应用限定的读取身份：指标和日志强制过滤，Trace 只能按 ID 读取，不能搜索。"""
        readers = json.dumps([{"name": "orders", "token": "s" * 40, "namespace": "orders",
                               "apps": ["orders-workers", "orders-scheduler"]}])
        users = auth_config({"LUMA_OBSERVE_SCOPED_READERS": readers})["users"]
        self.assertEqual([user["name"] for user in users], ["observe-scoped-orders"])
        routes = {route["src_paths"][0]: route for route in users[0]["url_map"]}
        self.assertTrue(routes[r"/metrics/api/v1/query"]["url_prefix"].endswith("?extra_label=service_namespace=orders"))
        logs = urllib.parse.unquote(routes[r"/logs/select/logsql/query"]["url_prefix"].split("extra_filters=", 1)[1])
        self.assertEqual(logs, 'app:in("orders-workers","orders-scheduler")')
        patterns = [path for route in users[0]["url_map"] for path in route["src_paths"]]
        self.assertTrue(any(re.fullmatch(pattern, "/traces/api/traces/" + "a" * 32) for pattern in patterns))
        for path in ("/traces/api/search", "/traces/api/search/tags", "/otlp/v1/traces", "/metrics/api/v1/series"):
            self.assertFalse(any(re.fullmatch(pattern, path) for pattern in patterns), path)

    def test_scoped_readers_reject_invalid_scopes_and_reused_tokens_without_leaking(self):
        base = {"name": "orders", "token": "s" * 40, "namespace": "orders", "apps": ["orders-workers"]}
        cases = [
            "not-json", json.dumps({"name": "x"}), json.dumps([{**base, "extra": 1}]),
            json.dumps([{**base, "namespace": 'orders",x="1'}]), json.dumps([{**base, "apps": ['a") OR *']}]),
            json.dumps([{**base, "apps": []}]), json.dumps([base, {**base, "token": "t" * 40}]),
            json.dumps([{**base, "token": "short"}]),
        ]
        for raw in cases:
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(ValueError) as result:
                    auth_config({"LUMA_OBSERVE_SCOPED_READERS": raw})
                self.assertNotIn("s" * 40, str(result.exception))
        with self.assertRaises(ValueError):
            auth_config({"LUMA_OBSERVE_READ_TOKEN": "s" * 40, "LUMA_OBSERVE_SCOPED_READERS": json.dumps([base])})

if __name__ == "__main__":
    unittest.main()
