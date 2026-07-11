from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from lae_luma_adapter import (
    BuilderLimits,
    HttpLumaBuilderAdapter,
    HttpLumaRuntimeAdapter,
    LumaBuilderAdapter,
    RuntimeServicePrincipal,
    ServicePrincipal,
)
from lae_store import (
    EnvironmentKeyRing,
    OperationRecord,
    OperationStore,
    S3PrivateObjectConfig,
    S3PrivateObjectStore,
    create_postgres_engine,
    create_session_factory,
)

from .analyze import (
    AnalysisResultRecorder,
    AnalyzeStateStore,
    AnalyzeStepRunner,
    AnalyzeWorker,
    AnalyzeWorkerConfig,
    PostgresAnalyzeContextLoader,
)
from .artifact_runtime import artifact_recorder_from_env
from .build_plan_materializer import (
    HmacBuildCredentialLeaseIssuer,
    S3TrustedBuildPlanMaterializer,
)
from .deployment import (
    DeploymentStepRunner,
    DeploymentWorker,
    DeploymentWorkerConfig,
    RuntimeManifestRenderer,
)
from .deployment_postgres import (
    PostgresDeploymentContextLoader,
    PostgresDeploymentStateStore,
)
from .postgres import (
    PostgresAnalysisRecorder,
    PostgresAnalyzeStateStore,
    PostgresUpdateCheckResolver,
)
from .runtime_secrets import (
    HttpEphemeralRuntimeSecretIssuer,
    PostgresRuntimeSecretProvider,
)
from .lifecycle import (
    LifecycleStepRunner,
    LifecycleWorker,
    LifecycleWorkerConfig,
    PostgresLifecycleContextLoader,
    PostgresLifecycleStateStore,
)
from .static_upload import (
    StaticUploadScanner,
    StaticUploadScannerRuntime,
    build_static_upload_scanner_from_env,
)


class WorkerLaneFailure(RuntimeError):
    """Stable, secret-free top-level failure for one concurrent worker lane."""


@dataclass(frozen=True, slots=True)
class WorkerRunSummary:
    operation_results: tuple[OperationRecord, ...]
    upload_scanned: bool = False
    upload_cleaned: bool = False

    @property
    def idle(self) -> bool:
        return not self.operation_results and not self.upload_scanned and not self.upload_cleaned


class UnifiedWorker:
    """Run one bounded unit from every enabled lane without starvation.

    Analyze and deployment runners each retain their operation lease until a
    terminal state, but they execute concurrently. A slow build can therefore
    never prevent source analysis or upload scanning from making progress.
    """

    def __init__(
        self,
        analyze_worker: AnalyzeWorker,
        *,
        deployment_worker: DeploymentWorker | None = None,
        lifecycle_worker: LifecycleWorker | None = None,
        upload_scanner: StaticUploadScanner | None = None,
    ) -> None:
        self.analyze_worker = analyze_worker
        self.deployment_worker = deployment_worker
        self.lifecycle_worker = lifecycle_worker
        self.upload_scanner = upload_scanner
        # Compatibility for existing diagnostic tests and operators that
        # inspect the verified analysis recorder through the worker runtime.
        self._runner = analyze_worker._runner

    async def run_once(self) -> WorkerRunSummary:
        lanes: list[tuple[str, Any]] = [
            ("source.analyze", self.analyze_worker.run_once()),
        ]
        if self.deployment_worker is not None:
            lanes.append(("deployment.create", self.deployment_worker.run_once()))
        if self.lifecycle_worker is not None:
            lanes.append(("application.lifecycle", self.lifecycle_worker.run_once()))
        if self.upload_scanner is not None:
            lanes.append(("upload", self._run_upload_once()))
        values = await asyncio.gather(
            *(coroutine for _name, coroutine in lanes),
            return_exceptions=True,
        )
        if any(isinstance(value, BaseException) for value in values):
            # Do not include exception text: upstream/S3/database failures may
            # contain URLs or credentials. Operation leases expire and are
            # safely reclaimed by a later worker process.
            raise WorkerLaneFailure("a worker lane failed closed") from None
        operations: list[OperationRecord] = []
        upload_scanned = False
        upload_cleaned = False
        for (name, _coroutine), value in zip(lanes, values, strict=True):
            if name == "upload":
                upload_scanned, upload_cleaned = value
            elif value is not None:
                operations.append(value.operation)
        return WorkerRunSummary(
            operation_results=tuple(operations),
            upload_scanned=upload_scanned,
            upload_cleaned=upload_cleaned,
        )

    async def _run_upload_once(self) -> tuple[bool, bool]:
        assert self.upload_scanner is not None
        scanned = await self.upload_scanner.run_once()
        cleaned = await self.upload_scanner.cleanup_once()
        return scanned, cleaned


@dataclass(slots=True)
class WorkerRuntime:
    worker: UnifiedWorker
    engine: object | None = None
    auxiliary_runtimes: tuple[object, ...] = ()

    async def close(self) -> None:
        closed: set[int] = set()
        for resource in (*self.auxiliary_runtimes, self.engine):
            if resource is None or id(resource) in closed:
                continue
            closed.add(id(resource))
            close = getattr(resource, "close", None)
            if close is not None:
                result = close()
                if result is not None:
                    await result
                continue
            dispose = getattr(resource, "dispose", None)
            if dispose is not None:
                await dispose()


def build_analyze_worker(
    *,
    operations: OperationStore,
    sessions: object,
    states: AnalyzeStateStore,
    luma: LumaBuilderAdapter,
    config: AnalyzeWorkerConfig,
    worker_id: str,
    recorder: AnalysisResultRecorder | None = None,
) -> AnalyzeWorker:
    contexts = PostgresAnalyzeContextLoader(sessions)
    runner = AnalyzeStepRunner(
        operations=operations,
        contexts=contexts,
        states=states,
        luma=luma,
        config=config,
        worker_id=worker_id,
        recorder=recorder,
        update_checks=PostgresUpdateCheckResolver(sessions),
    )
    return AnalyzeWorker(
        operations,
        runner,
        worker_id=worker_id,
        config=config,
    )


def build_worker_from_env(
    *,
    states: AnalyzeStateStore | None = None,
    recorder: AnalysisResultRecorder | None = None,
    environ: Mapping[str, str] | None = None,
) -> WorkerRuntime:
    """Wire all explicitly enabled production lanes from closed configuration."""

    values = os.environ if environ is None else environ
    environment = values.get("LAE_ENVIRONMENT", "development").strip().lower()
    production = environment in {"production", "prod"}
    artifact_driver = values.get("LAE_ARTIFACT_DRIVER", "disabled").strip().lower()
    if production and recorder is None and artifact_driver != "s3":
        raise ValueError("production requires a secure verified artifact-ingest recorder")
    if artifact_driver not in {"disabled", "s3"}:
        raise ValueError("LAE_ARTIFACT_DRIVER is unsupported")
    database_url = _required_env(values, "LAE_DATABASE_URL")
    endpoint = _required_env(values, "LAE_LUMA_CONTROL_URL")
    principal_id = _required_env(values, "LAE_LUMA_SERVICE_PRINCIPAL_ID")
    token = _required_env(values, "LAE_LUMA_SERVICE_TOKEN")
    worker_id = values.get("LAE_WORKER_ID", "lae-worker-1").strip()
    lease_seconds = _integer(values, "LAE_WORKER_LEASE_SECONDS", 60)
    config = AnalyzeWorkerConfig(
        agent_image_digest=_required_env(values, "LAE_ANALYZER_IMAGE_DIGEST"),
        policy_version=values.get("LAE_ANALYSIS_POLICY_VERSION", "2026-07-11"),
        limits=BuilderLimits(
            cpu=_float(values, "LAE_ANALYSIS_CPU", 2.0),
            memory_mib=_integer(values, "LAE_ANALYSIS_MEMORY_MIB", 2048),
            disk_mib=_integer(values, "LAE_ANALYSIS_DISK_MIB", 4096),
            timeout_seconds=_integer(values, "LAE_ANALYSIS_TIMEOUT_SECONDS", 300),
        ),
        lease_seconds=lease_seconds,
        event_page_limit=_integer(values, "LAE_WORKER_EVENT_PAGE_LIMIT", 100),
        poll_interval_seconds=_float(values, "LAE_WORKER_POLL_SECONDS", 1.0),
    )
    engine = create_postgres_engine(database_url)
    sessions = create_session_factory(engine)
    operations = OperationStore(sessions)
    if states is None:
        states = PostgresAnalyzeStateStore(
            sessions,
            luma_cluster_id=_required_env(values, "LAE_LUMA_CLUSTER_ID"),
            luma_principal_id=principal_id,
            hash_key=_decode_hmac_key(
                _required_env(values, "LAE_WORKER_STATE_HMAC_KEY"),
                label="LAE_WORKER_STATE_HMAC_KEY",
            ),
            hash_key_version=_integer(values, "LAE_WORKER_STATE_HMAC_KEY_VERSION", 1),
            credential_lease_ttl=timedelta(
                seconds=_integer(values, "LAE_SOURCE_LEASE_TTL_SECONDS", 900)
            ),
        )
    if recorder is None and artifact_driver == "s3":
        recorder = artifact_recorder_from_env(
            sessions=sessions,
            operations=operations,
            agent_image_digest=config.agent_image_digest,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            environ=values,
        )
    if recorder is None:
        recorder = PostgresAnalysisRecorder(
            sessions, agent_image_digest=config.agent_image_digest
        )
    if production and not bool(getattr(recorder, "stores_verified_artifacts", False)):
        raise ValueError("production requires a secure verified artifact-ingest recorder")
    luma = HttpLumaBuilderAdapter(
        endpoint,
        ServicePrincipal(principal_id, token),
        timeout_seconds=min(
            _float(values, "LAE_LUMA_HTTP_TIMEOUT_SECONDS", 20.0),
            max(float(lease_seconds) / 2, 1.0),
        ),
    )
    analyze_worker = build_analyze_worker(
        operations=operations,
        sessions=sessions,
        states=states,
        luma=luma,
        config=config,
        worker_id=worker_id,
        recorder=recorder,
    )

    deployment_worker: DeploymentWorker | None = None
    lifecycle_worker: LifecycleWorker | None = None
    runtime_adapter: HttpLumaRuntimeAdapter | None = None
    if _boolean(values, "LAE_DEPLOYMENT_WORKER_ENABLED", False):
        if artifact_driver != "s3":
            raise ValueError("deployment worker requires verified S3 artifacts")
        runtime_principal_id = _required_env(
            values, "LAE_LUMA_RUNTIME_PRINCIPAL_ID"
        )
        runtime_token = _required_env(
            values, "LAE_LUMA_RUNTIME_SERVICE_TOKEN"
        )
        if runtime_principal_id == principal_id or runtime_token == token:
            raise ValueError("builder and runtime service principals must be independent")
        runtime_endpoint = values.get("LAE_LUMA_RUNTIME_URL", endpoint).strip()
        runtime_principal = RuntimeServicePrincipal(
            runtime_principal_id, runtime_token
        )
        deployment_config = DeploymentWorkerConfig(
            build_limits=BuilderLimits(
                cpu=_float(values, "LAE_BUILD_CPU", 4.0),
                memory_mib=_integer(values, "LAE_BUILD_MEMORY_MIB", 4096),
                disk_mib=_integer(values, "LAE_BUILD_DISK_MIB", 16384),
                timeout_seconds=_integer(values, "LAE_BUILD_TIMEOUT_SECONDS", 1800),
            ),
            lease_seconds=lease_seconds,
            timeout_seconds=_integer(values, "LAE_DEPLOYMENT_TIMEOUT_SECONDS", 3600),
            event_page_limit=_integer(values, "LAE_WORKER_EVENT_PAGE_LIMIT", 100),
            poll_interval_seconds=_float(values, "LAE_WORKER_POLL_SECONDS", 1.0),
        )
        materializer = S3TrustedBuildPlanMaterializer(
            _private_object_store_from_env(values),
            signing_key_id=_required_env(values, "LAE_BUILD_PLAN_SIGNING_KEY_ID"),
            signing_key=_decode_hmac_key(
                _required_env(values, "LAE_BUILD_PLAN_SIGNING_HMAC_KEY"),
                label="LAE_BUILD_PLAN_SIGNING_HMAC_KEY",
            ),
            lease_issuer=HmacBuildCredentialLeaseIssuer(
                _decode_hmac_key(
                    _required_env(
                        values, "LAE_BUILD_CREDENTIAL_LEASE_HMAC_KEY"
                    ),
                    label="LAE_BUILD_CREDENTIAL_LEASE_HMAC_KEY",
                )
            ),
            timeout_seconds=_float(
                values, "LAE_BUILD_PLAN_MATERIALIZE_TIMEOUT_SECONDS", 30.0
            ),
        )
        contexts = PostgresDeploymentContextLoader(
            sessions,
            materializer,
            region=values.get("LAE_DEPLOYMENT_REGION", "cn").strip(),
        )
        state_store = PostgresDeploymentStateStore(
            sessions,
            luma_cluster_id=_required_env(values, "LAE_LUMA_CLUSTER_ID"),
        )
        secret_issuer = HttpEphemeralRuntimeSecretIssuer(
            runtime_endpoint,
            runtime_principal,
            production=production,
            timeout_seconds=_float(
                values, "LAE_LUMA_RUNTIME_HTTP_TIMEOUT_SECONDS", 10.0
            ),
        )
        secret_provider = PostgresRuntimeSecretProvider(
            sessions,
            _environment_key_ring_from_env(values),
            secret_issuer,
            ttl_seconds=_integer(values, "LAE_RUNTIME_SECRET_TTL_SECONDS", 60),
        )
        runtime_adapter = HttpLumaRuntimeAdapter(
            runtime_endpoint,
            runtime_principal,
            timeout_seconds=_float(
                values, "LAE_LUMA_RUNTIME_HTTP_TIMEOUT_SECONDS", 20.0
            ),
        )
        runner = DeploymentStepRunner(
            operations=operations,
            contexts=contexts,
            states=state_store,
            builder=luma,
            runtime=runtime_adapter,
            secrets=secret_provider,
            renderer=RuntimeManifestRenderer(),
            config=deployment_config,
            worker_id=worker_id,
        )
        deployment_worker = DeploymentWorker(
            operations,
            runner,
            worker_id=worker_id,
            config=deployment_config,
        )

    if _boolean(
        values,
        "LAE_LIFECYCLE_WORKER_ENABLED",
        deployment_worker is not None,
    ):
        if runtime_adapter is None:
            raise ValueError("lifecycle worker requires the deployment runtime adapter")
        lifecycle_config = LifecycleWorkerConfig(
            lease_seconds=lease_seconds,
            timeout_seconds=_integer(
                values, "LAE_LIFECYCLE_TIMEOUT_SECONDS", 1800
            ),
            poll_interval_seconds=_float(values, "LAE_WORKER_POLL_SECONDS", 1.0),
        )
        lifecycle_runner = LifecycleStepRunner(
            operations=operations,
            contexts=PostgresLifecycleContextLoader(
                sessions,
                luma_cluster_id=_required_env(values, "LAE_LUMA_CLUSTER_ID"),
            ),
            states=PostgresLifecycleStateStore(sessions),
            runtime=runtime_adapter,
            config=lifecycle_config,
            worker_id=worker_id,
        )
        lifecycle_worker = LifecycleWorker(
            operations,
            lifecycle_runner,
            worker_id=worker_id,
            config=lifecycle_config,
        )

    upload_runtime: StaticUploadScannerRuntime | None = None
    if values.get("LAE_UPLOAD_SCANNER_ENABLED") == "1":
        upload_runtime = build_static_upload_scanner_from_env(values)
    worker = UnifiedWorker(
        analyze_worker,
        deployment_worker=deployment_worker,
        lifecycle_worker=lifecycle_worker,
        upload_scanner=None if upload_runtime is None else upload_runtime.scanner,
    )
    return WorkerRuntime(
        worker=worker,
        engine=engine,
        auxiliary_runtimes=() if upload_runtime is None else (upload_runtime,),
    )


def _private_object_store_from_env(
    values: Mapping[str, str],
) -> S3PrivateObjectStore:
    environment = values.get("LAE_ENVIRONMENT", "development").strip().lower()
    endpoint = _required_env(values, "LAE_ARTIFACT_S3_ENDPOINT")
    return S3PrivateObjectStore(
        S3PrivateObjectConfig(
            endpoint=endpoint,
            bucket=_required_env(values, "LAE_ARTIFACT_S3_BUCKET"),
            region=_required_env(values, "LAE_ARTIFACT_S3_REGION"),
            access_key=_required_env(values, "LAE_ARTIFACT_S3_ACCESS_KEY"),
            secret_key=_required_env(values, "LAE_ARTIFACT_S3_SECRET_KEY"),
            allowed_hosts=tuple(
                item.strip()
                for item in _required_env(
                    values, "LAE_ARTIFACT_S3_ALLOWED_HOSTS"
                ).split(",")
                if item.strip()
            ),
            path_style=_boolean(values, "LAE_ARTIFACT_S3_PATH_STYLE", True),
            production=environment in {"production", "prod"},
            timeout_seconds=_float(values, "LAE_ARTIFACT_S3_TIMEOUT_SECONDS", 20.0),
        )
    )


def _environment_key_ring_from_env(
    values: Mapping[str, str],
) -> EnvironmentKeyRing:
    try:
        current = int(_required_env(values, "LAE_ENVIRONMENT_AEAD_KEY_VERSION"))
        raw = json.loads(_required_env(values, "LAE_ENVIRONMENT_AEAD_KEYS"))
        if not isinstance(raw, Mapping):
            raise ValueError("environment key ring must be an object")
        keys = {
            int(version): _decode_exact_key(encoded, label="environment AEAD key")
            for version, encoded in raw.items()
            if isinstance(encoded, str)
        }
        if len(keys) != len(raw):
            raise ValueError("environment AEAD key ring is invalid")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("environment AEAD key ring is invalid") from exc
    return EnvironmentKeyRing(
        current_version=current,
        keys=keys,
        checksum_key=_decode_hmac_key(
            _required_env(values, "LAE_ENVIRONMENT_CHECKSUM_HMAC_KEY"),
            label="LAE_ENVIRONMENT_CHECKSUM_HMAC_KEY",
        ),
    )


def _required_env(values: Mapping[str, str], key: str) -> str:
    value = values.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _integer(values: Mapping[str, str], key: str, default: int) -> int:
    value = values.get(key)
    return default if value is None else int(value)


def _float(values: Mapping[str, str], key: str, default: float) -> float:
    value = values.get(key)
    return default if value is None else float(value)


def _boolean(values: Mapping[str, str], key: str, default: bool) -> bool:
    value = values.get(key)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"{key} must be a boolean")


def _decode_hmac_key(value: str, *, label: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{label} must be valid base64") from exc
    if len(key) < 32:
        raise ValueError(f"{label} must contain at least 256 bits")
    return key


def _decode_exact_key(value: str, *, label: str) -> bytes:
    key = _decode_hmac_key(value, label=label)
    if len(key) != 32:
        raise ValueError(f"{label} must contain exactly 256 bits")
    return key


__all__ = [
    "UnifiedWorker",
    "WorkerLaneFailure",
    "WorkerRunSummary",
    "WorkerRuntime",
    "build_analyze_worker",
    "build_worker_from_env",
]
