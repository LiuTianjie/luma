"""为标准 vmauth 进程准备独立读取和 OTLP 写入配置。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile

TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+={0,2}\Z")


def auth_config(environment: dict[str, str]) -> dict:
    """按已配置凭证启用固定路由，空配置以零用户启动。"""
    read_token = environment.get("LUMA_OBSERVE_READ_TOKEN", "")
    ingest_token = environment.get("LUMA_OBSERVE_INGEST_TOKEN", "")
    for name, value in (("LUMA_OBSERVE_READ_TOKEN", read_token), ("LUMA_OBSERVE_INGEST_TOKEN", ingest_token)):
        if value and (not 32 <= len(value) <= 512 or not TOKEN.fullmatch(value)):
            raise ValueError(f"{name} must be a 32-512 character bearer token")
    if read_token and read_token == ingest_token:
        raise ValueError("read and ingest credentials must be distinct")
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
