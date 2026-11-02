"""验证观测读取与 OTLP 写入的配置边界及安全关闭状态。"""
from pathlib import Path
import re
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

if __name__ == "__main__":
    unittest.main()
