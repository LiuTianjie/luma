from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..errors import LumaError


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise LumaError(f"{name} is required for the build platform")
    return value


def _integer(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise LumaError(f"{name} must be an integer") from exc
    if not low <= value <= high:
        raise LumaError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_token: str
    encryption_key: str
    public_url: str
    control_url: str
    control_token: str
    temporal_address: str
    temporal_namespace: str
    task_queue: str
    nomad_url: str
    nomad_token: str
    nomad_namespace: str
    executor_image: str
    registry_url: str
    registry_username: str
    registry_password: str
    registry_pull_username: str
    registry_pull_password: str
    registry_project: str
    s3_endpoint: str
    s3_bucket: str
    s3_region: str
    max_active: int
    max_pending: int
    max_per_project: int
    cpu: int
    memory_mb: int
    disk_mb: int
    build_pool: str
    placement_timeout: int = 60
    startup_timeout: int = 600

    @property
    def registry_host(self) -> str:
        return urlsplit(self.registry_url).netloc

    @classmethod
    def from_env(cls) -> "Settings":
        registry = _required("LUMA_BUILD_REGISTRY_URL").rstrip("/")
        public = _required("LUMA_BUILD_PUBLIC_URL").rstrip("/")
        for name, value in (("LUMA_BUILD_REGISTRY_URL", registry), ("LUMA_BUILD_PUBLIC_URL", public)):
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise LumaError(f"{name} must be an HTTP(S) URL without inline credentials")
        return cls(
            database_url=_required("LUMA_BUILD_DATABASE_URL"),
            api_token=_required("LUMA_BUILD_API_TOKEN"),
            encryption_key=_required("LUMA_BUILD_ENCRYPTION_KEY"),
            public_url=public, control_url=_required("LUMA_CONTROL_URL").rstrip("/"),
            control_token=_required("LUMA_DEPLOY_TOKEN"),
            temporal_address=os.environ.get("LUMA_TEMPORAL_ADDRESS", "temporal:7233"),
            temporal_namespace=os.environ.get("LUMA_TEMPORAL_NAMESPACE", "default"),
            task_queue=os.environ.get("LUMA_BUILD_TASK_QUEUE", "luma-builds"),
            nomad_url=_required("LUMA_NOMAD_URL").rstrip("/"),
            nomad_token=os.environ.get("LUMA_NOMAD_TOKEN", ""),
            nomad_namespace=os.environ.get("LUMA_NOMAD_NAMESPACE", "default"),
            executor_image=_required("LUMA_BUILD_EXECUTOR_IMAGE"), registry_url=registry,
            registry_username=_required("LUMA_BUILD_REGISTRY_USERNAME"),
            registry_password=_required("LUMA_BUILD_REGISTRY_PASSWORD"),
            registry_pull_username=_required("LUMA_BUILD_REGISTRY_PULL_USERNAME"),
            registry_pull_password=_required("LUMA_BUILD_REGISTRY_PULL_PASSWORD"),
            registry_project=os.environ.get("LUMA_BUILD_REGISTRY_PROJECT", "luma"),
            s3_endpoint=os.environ.get("LUMA_BUILD_S3_ENDPOINT", ""),
            s3_bucket=_required("LUMA_BUILD_S3_BUCKET"),
            s3_region=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            max_active=_integer("LUMA_BUILD_MAX_ACTIVE", 10, 1, 100),
            max_pending=_integer("LUMA_BUILD_MAX_PENDING", 100, 1, 10000),
            max_per_project=_integer("LUMA_BUILD_MAX_PER_PROJECT", 10, 1, 100),
            cpu=_integer("LUMA_BUILD_CPU_MHZ", 2000, 100, 100000),
            memory_mb=_integer("LUMA_BUILD_MEMORY_MB", 4096, 256, 262144),
            disk_mb=_integer("LUMA_BUILD_DISK_MB", 8192, 512, 1048576),
            startup_timeout=_integer("LUMA_BUILD_STARTUP_TIMEOUT", 600, 60, 3600),
            placement_timeout=_integer("LUMA_BUILD_PLACEMENT_TIMEOUT", 60, 10, 300),
            build_pool=os.environ.get("LUMA_BUILD_POOL", "default"),
        )
