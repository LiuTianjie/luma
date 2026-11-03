"""为标准 vmauth 进程准备独立读取和 OTLP 写入配置。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import quote

TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+={0,2}\Z")
# Namespaces, reader names and Luma application names used inside enforced filters.
SCOPE_VALUE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")


def _valid_token(name: str, value: str) -> None:
    if not 32 <= len(value) <= 512 or not TOKEN.fullmatch(value):
        raise ValueError(f"{name} must be a 32-512 character bearer token")


def scoped_readers(raw: str) -> list[dict]:
    """解析按应用限定范围的读取身份；错误信息只包含字段名，不包含凭证。"""
    if not raw.strip():
        return []
    try:
        readers = json.loads(raw)
    except ValueError:
        raise ValueError("LUMA_OBSERVE_SCOPED_READERS must be a JSON list") from None
    if not isinstance(readers, list) or len(readers) > 32:
        raise ValueError("LUMA_OBSERVE_SCOPED_READERS must be a JSON list of at most 32 readers")
    result, names = [], set()
    for index, reader in enumerate(readers):
        field = f"LUMA_OBSERVE_SCOPED_READERS[{index}]"
        if not isinstance(reader, dict) or set(reader) != {"name", "token", "namespace", "apps"}:
            raise ValueError(f"{field} needs exactly name, token, namespace and apps")
        name, token, namespace, apps = reader["name"], reader["token"], reader["namespace"], reader["apps"]
        if not isinstance(name, str) or not SCOPE_VALUE.fullmatch(name) or name in names:
            raise ValueError(f"{field}.name must be a unique short identifier")
        if not isinstance(token, str):
            raise ValueError(f"{field}.token must be a string")
        _valid_token(f"{field}.token", token)
        if not isinstance(namespace, str) or not SCOPE_VALUE.fullmatch(namespace):
            raise ValueError(f"{field}.namespace must be a short identifier")
        if (not isinstance(apps, list) or not 1 <= len(apps) <= 16
                or not all(isinstance(app, str) and SCOPE_VALUE.fullmatch(app) for app in apps)):
            raise ValueError(f"{field}.apps must list 1-16 Luma application names")
        names.add(name)
        result.append({"name": name, "token": token, "namespace": namespace, "apps": list(dict.fromkeys(apps))})
    return result


def scoped_reader_user(reader: dict) -> dict:
    """指标强制限定 service.namespace，日志强制限定 Luma 应用，Trace 只允许按 ID 读取。

    vmauth 在 url_prefix 中设置的参数不能被客户端覆盖；按 ID 读取不提供枚举能力，
    读取方仍需自行校验返回 Span 的资源归属。
    """
    apps = ",".join(json.dumps(app) for app in reader["apps"])
    return {
        "name": "observe-scoped-" + reader["name"], "bearer_token": reader["token"], "max_concurrent_requests": 4,
        "headers": ["Authorization:"],
        "url_map": [
            {"src_paths": [r"/metrics/api/v1/query", r"/metrics/api/v1/query_range"],
             "url_prefix": f"http://127.0.0.1:8428/?extra_label=service_namespace={reader['namespace']}",
             "drop_src_path_prefix_parts": 1},
            {"src_paths": [r"/logs/select/logsql/query"],
             "url_prefix": "http://127.0.0.1:9428/?extra_filters=" + quote(f"app:in({apps})", safe=""),
             "drop_src_path_prefix_parts": 1},
            {"src_paths": [r"/traces/api/traces/[0-9a-fA-F]{32}"],
             "url_prefix": "http://127.0.0.1:3200", "drop_src_path_prefix_parts": 1},
        ],
    }


def auth_config(environment: dict[str, str]) -> dict:
    """按已配置凭证启用固定路由，空配置以零用户启动。"""
    read_token = environment.get("LUMA_OBSERVE_READ_TOKEN", "")
    ingest_token = environment.get("LUMA_OBSERVE_INGEST_TOKEN", "")
    for name, value in (("LUMA_OBSERVE_READ_TOKEN", read_token), ("LUMA_OBSERVE_INGEST_TOKEN", ingest_token)):
        if value:
            _valid_token(name, value)
    if read_token and read_token == ingest_token:
        raise ValueError("read and ingest credentials must be distinct")
    readers = scoped_readers(environment.get("LUMA_OBSERVE_SCOPED_READERS", ""))
    tokens = [token for token in (read_token, ingest_token) if token] + [reader["token"] for reader in readers]
    if len(tokens) != len(set(tokens)):
        raise ValueError("each observe credential must be distinct")
    users = []
    if read_token:
        users.append({
            "name": "observe-reader", "bearer_token": read_token, "max_concurrent_requests": 4,
            "headers": ["Authorization:"],
            "url_map": [
                {"src_paths": [r"/metrics/api/v1/query", r"/metrics/api/v1/query_range"],
                 "url_prefix": "http://127.0.0.1:8428", "drop_src_path_prefix_parts": 1},
                {"src_paths": [r"/logs/select/logsql/query"],
                 "url_prefix": "http://127.0.0.1:9428", "drop_src_path_prefix_parts": 1},
                {"src_paths": [r"/traces/api/search", r"/traces/api/traces/[0-9a-fA-F]{32}"],
                 "url_prefix": "http://127.0.0.1:3200", "drop_src_path_prefix_parts": 1},
            ],
        })
    if ingest_token:
        users.append({
            "name": "observe-ingester", "bearer_token": ingest_token, "max_concurrent_requests": 4,
            "headers": ["Authorization:"],
            "url_map": [{"src_paths": [r"/otlp/v1/traces", r"/otlp/v1/metrics"],
                         "url_prefix": "http://127.0.0.1:4318", "drop_src_path_prefix_parts": 1}],
        })
    users.extend(scoped_reader_user(reader) for reader in readers)
    return {"users": users}


def main() -> None:
    """写入进程私有配置后交由官方 vmauth 提供代理与认证。"""
    try:
        config = auth_config(dict(os.environ))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    with tempfile.NamedTemporaryFile(mode="w", prefix="vmauth-", suffix=".json", delete=False) as stream:
        json.dump(config, stream)
        path = Path(stream.name)
    os.execv("/vmauth-prod", ["/vmauth-prod", "-auth.config=" + str(path), *sys.argv[1:]])


if __name__ == "__main__":
    main()
