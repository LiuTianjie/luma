#!/usr/bin/env python3
"""Generate a one-time LAE staging secret and Luma Control configuration bundle.

The command never prints secret values and refuses to overwrite an existing
directory.  The resulting directory is an operator hand-off artifact, not a
repository asset: copy only the documented files to the manager and import the
generated ``lae-platform-staging.env`` through Luma's secret store.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit


_IMAGE_DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_CLUSTER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_STORAGE_CLASS = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_REGISTRY_HOST = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,252}$")
_NODE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _base64_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(36)


def _closed_https_url(value: str, *, label: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} must be a closed HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise ValueError(f"{label} must be a closed HTTPS URL")
    return value.rstrip("/")


def _write_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            target.write(content)
            if not content.endswith("\n"):
                target.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _dotenv(values: dict[str, str]) -> str:
    lines: list[str] = []
    for name in sorted(values):
        value = values[name]
        if any(character in value for character in "\x00\r\n"):
            raise ValueError(f"{name} cannot be represented in dotenv")
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a private LAE staging deployment bundle."
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--analyzer-image-digest", required=True)
    parser.add_argument("--cluster-id", required=True)
    parser.add_argument("--control-url", default="https://luma.itool.tech")
    parser.add_argument(
        "--public-api-url", default="https://lae-api-staging.itool.tech"
    )
    parser.add_argument(
        "--checkout-base-url", default="https://lae-staging.itool.tech"
    )
    parser.add_argument(
        "--agent-controller-url",
        default="https://lae-agent-staging.itool.tech",
    )
    parser.add_argument(
        "--llm-base-url", default="https://ark.cn-beijing.volces.com/api/v3"
    )
    parser.add_argument("--llm-model", required=True)
    parser.add_argument(
        "--llm-api-key-file",
        required=True,
        help="Root/user-owned 0400 or 0600 file containing the staging provider API key.",
    )
    parser.add_argument(
        "--runtime-storage-class", default="lae-staging-runtime-nfs"
    )
    parser.add_argument(
        "--runtime-node",
        action="append",
        dest="runtime_nodes",
        default=None,
        help=(
            "Internal positive-admission LAE runner name; repeat as needed "
            "(staging defaults: manager, tecent)."
        ),
    )
    parser.add_argument(
        "--external-registry",
        action="append",
        dest="external_registries",
        default=None,
        help="Exact public registry host; repeat as needed (default: docker.io, ghcr.io).",
    )
    return parser.parse_args(argv)


def generate(argv: list[str] | None = None) -> dict[str, object]:
    args = _arguments(argv)
    if _IMAGE_DIGEST.fullmatch(args.analyzer_image_digest) is None:
        raise ValueError("analyzer image must be an immutable repository@sha256 digest")
    if _CLUSTER_ID.fullmatch(args.cluster_id) is None:
        raise ValueError("cluster id is invalid")
    if _STORAGE_CLASS.fullmatch(args.runtime_storage_class) is None:
        raise ValueError("runtime storage class is invalid")
    control_url = _closed_https_url(args.control_url, label="control URL")
    public_api_url = _closed_https_url(args.public_api_url, label="public API URL")
    checkout_base_url = _closed_https_url(
        args.checkout_base_url, label="checkout base URL"
    )
    agent_controller_url = _closed_https_url(
        args.agent_controller_url, label="agent controller URL"
    )
    llm_base_url = _closed_https_url(args.llm_base_url, label="LLM base URL")
    key_path = Path(args.llm_api_key_file).expanduser()
    key_stat = key_path.lstat()
    if not key_path.is_file() or key_path.is_symlink():
        raise ValueError("LLM API key file must be a regular non-symlink file")
    if key_stat.st_uid not in {0, os.getuid()} or key_stat.st_mode & 0o077 or key_stat.st_size > 16 * 1024:
        raise ValueError("LLM API key file owner, mode, or size is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(key_path, flags)
    try:
        llm_api_key = os.read(descriptor, 16 * 1024 + 1).decode("utf-8").strip()
    finally:
        os.close(descriptor)
    if not llm_api_key or any(character in llm_api_key for character in "\x00\r\n"):
        raise ValueError("LLM API key file is invalid")
    if not args.llm_model.strip():
        raise ValueError("LLM model is required")
    registries = sorted(args.external_registries or ["docker.io", "ghcr.io"])
    if (
        not registries
        or len(registries) > 32
        or len(registries) != len(set(registries))
        or any(_REGISTRY_HOST.fullmatch(item) is None for item in registries)
    ):
        raise ValueError("external registries must be unique lowercase exact hosts")
    runtime_nodes = sorted(args.runtime_nodes or ["manager", "tecent"])
    if (
        not runtime_nodes
        or len(runtime_nodes) > 64
        or len(runtime_nodes) != len(set(runtime_nodes))
        or any(_NODE_NAME.fullmatch(item) is None for item in runtime_nodes)
    ):
        raise ValueError("runtime nodes must be unique exact node names")

    output = Path(args.output_dir).expanduser().resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    output.chmod(0o700)

    builder_token = _token("lae_builder_")
    runtime_token = _token("lae_runtime_")
    credential_broker_token = _token("lae_git_broker_")
    object_broker_token = _token("lae_object_broker_")
    admin_token = _token("lae_admin_")
    plan_signing_key = _base64_key()
    postgres_password = secrets.token_urlsafe(32)
    valkey_password = secrets.token_urlsafe(36)
    agent_controller_token = _token("lae_agent_")

    environment = {
        "LAE_ADMIN_API_TOKEN": admin_token,
        "LAE_ANALYZER_IMAGE_DIGEST": args.analyzer_image_digest,
        "LAE_AGENT_CONTROLLER_TOKEN": agent_controller_token,
        "LAE_APPLICATION_IDEMPOTENCY_HMAC_KEY": _base64_key(),
        "LAE_AUTH_HMAC_KEY": _base64_key(),
        "LAE_BILLING_HMAC_KEY": _base64_key(),
        "LAE_BUILD_CREDENTIAL_LEASE_HMAC_KEY": _base64_key(),
        "LAE_BUILD_PLAN_SIGNING_HMAC_KEY": plan_signing_key,
        "LAE_CREDENTIAL_BROKER_TOKEN": credential_broker_token,
        "LAE_DATABASE_URL": (
            "postgresql+asyncpg://lae:"
            + postgres_password
            + "@postgres:5432/lae"
        ),
        "LAE_DEPLOYMENT_IDEMPOTENCY_HMAC_KEY": _base64_key(),
        "LAE_EMAIL_FROM": "no-reply@staging.itool.tech",
        "LAE_ENVIRONMENT_AEAD_KEYS": json.dumps(
            {"1": _base64_key()}, separators=(",", ":")
        ),
        "LAE_ENVIRONMENT_CHECKSUM_HMAC_KEY": _base64_key(),
        "LAE_LUMA_CLUSTER_ID": args.cluster_id,
        "LAE_LUMA_CONTROL_URL": control_url,
        "LAE_LUMA_RUNTIME_SERVICE_TOKEN": runtime_token,
        "LAE_LUMA_SERVICE_TOKEN": builder_token,
        "LAE_MINIO_ROOT_PASSWORD": secrets.token_urlsafe(40),
        "LAE_MINIO_ROOT_USER": "lae-root",
        "LAE_MOCK_CHECKOUT_BASE_URL": checkout_base_url,
        "LAE_MOCK_PAYMENT_MERCHANT_ID": "lae-staging",
        "LAE_MOCK_PAYMENT_SIGNING_KEY": _base64_key(),
        "LAE_MOCK_PRICING_JSON": json.dumps(
            {
                "version": "mock-2026-07-11",
                "currency": "CNY",
                "plans": {
                    "pro": {"monthly": 2900, "yearly": 29000},
                    "ultra": {"monthly": 9900, "yearly": 99000},
                },
            },
            separators=(",", ":"),
        ),
        "LAE_OBJECT_SOURCE_BROKER_TOKEN": object_broker_token,
        "LAE_POSTGRES_PASSWORD": postgres_password,
        "LAE_S3_API_ACCESS_KEY": "LAEAPI" + secrets.token_hex(6).upper(),
        "LAE_S3_API_SECRET_KEY": secrets.token_urlsafe(36),
        "LAE_S3_WORKER_ACCESS_KEY": "LAEWORKER" + secrets.token_hex(5).upper(),
        "LAE_S3_WORKER_SECRET_KEY": secrets.token_urlsafe(36),
        "LAE_SMTP_HOST": "mailpit",
        "LAE_SMTP_PASSWORD": secrets.token_urlsafe(24),
        "LAE_SMTP_USERNAME": "lae-staging",
        "LAE_SOURCE_CONNECTION_AEAD_KEYS": json.dumps(
            {"1": _base64_key()}, separators=(",", ":")
        ),
        "LAE_SOURCE_CONNECTION_HMAC_KEYS": json.dumps(
            {"1": _base64_key()}, separators=(",", ":")
        ),
        "LAE_SOURCE_CONNECTION_IDEMPOTENCY_HMAC_KEY": _base64_key(),
        "LAE_UPLOAD_HMAC_KEY": _base64_key(),
        "LAE_VALKEY_PASSWORD": valkey_password,
        "LAE_WORKER_STATE_HMAC_KEY": _base64_key(),
        "VALKEY_PASSWORD": valkey_password,
    }
    environment.update(
        {
            "LAE_AGENT_LLM_API_KEY": llm_api_key,
            "LAE_AGENT_LLM_BASE_URL": llm_base_url,
            "LAE_AGENT_LLM_MODEL": args.llm_model.strip(),
        }
    )

    manager_files = {
        "lae-builder.token": builder_token,
        "lae-runtime.token": runtime_token,
        "credential-broker.token": credential_broker_token,
        "object-broker.token": object_broker_token,
        "lae-admin.token": admin_token,
        "builder-agent-ai.env": _dotenv(
            {
                "LUMA_BUILDER_ANALYZE_CONTROLLER_TOKEN": agent_controller_token,
                "LUMA_BUILDER_ANALYZE_CONTROLLER_URL": agent_controller_url,
                "LUMA_BUILDER_ANALYZE_AI_REQUIRED": "1",
            }
        ),
        "lae-builder-principals.json": json.dumps(
            {
                "lae-builder": {
                    "tokenFile": "lae-builder.token",
                    "tenantRefs": ["*"],
                    "applicationRefs": ["*"],
                }
            },
            separators=(",", ":"),
        ),
        "lae-runtime-principals.json": json.dumps(
            {
                "lae-runtime": {
                    "tokenFile": "lae-runtime.token",
                    "tenantRefs": ["*"],
                    "applicationRefs": ["*"],
                    "builderPrincipalRefs": ["lae-builder"],
                    "scopes": [
                        "runtime:volumes:prepare",
                        "runtime:deployments:write",
                        "runtime:deployments:read",
                        "runtime:secrets:issue",
                        "runtime:logs",
                        "runtime:metrics",
                    ],
                }
            },
            separators=(",", ":"),
        ),
        "lae-plan-signing.json": json.dumps(
            {"lae-plan-primary": "base64:" + plan_signing_key},
            separators=(",", ":"),
        ),
    }

    control_environment = {
        "LUMA_BUILDER_ANALYZE_IMAGE_DIGEST": args.analyzer_image_digest,
        "LUMA_CREDENTIAL_BROKER_TIMEOUT_SECONDS": "10",
        "LUMA_CREDENTIAL_BROKER_TOKEN_FILE": "/opt/luma/control/credential-broker.token",
        "LUMA_CREDENTIAL_BROKER_URL": public_api_url
        + "/v1/internal/credential-leases/redeem",
        "LUMA_LAE_ADMIN_API_URL": public_api_url,
        "LUMA_LAE_ADMIN_TIMEOUT_SECONDS": "10",
        "LUMA_LAE_ADMIN_TOKEN_FILE": "/opt/luma/control/lae-admin.token",
        "LUMA_LAE_BUILDER_ALLOW_ANONYMOUS_REGISTRY": "1",
        "LUMA_LAE_BUILDER_EXTERNAL_REGISTRIES_JSON": json.dumps(
            registries, separators=(",", ":")
        ),
        "LUMA_LAE_BUILDER_REGISTRY_INSECURE": "1",
        "LUMA_LAE_PLAN_SIGNING_KEYS_FILE": "/opt/luma/control/lae-plan-signing.json",
        "LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_FILE": "/opt/luma/control/lae-runtime-principals.json",
        "LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON": json.dumps(
            runtime_nodes, separators=(",", ":")
        ),
        "LUMA_LAE_RUNTIME_STORAGE_CLASS": args.runtime_storage_class,
        "LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS": "600",
        "LUMA_LAE_SERVICE_PRINCIPALS_FILE": "/opt/luma/control/lae-builder-principals.json",
        "LUMA_OBJECT_SOURCE_BROKER_TIMEOUT_SECONDS": "10",
        "LUMA_OBJECT_SOURCE_BROKER_TOKEN_FILE": "/opt/luma/control/object-broker.token",
        "LUMA_OBJECT_SOURCE_BROKER_URL": public_api_url
        + "/v1/internal/object-source-leases/redeem",
    }

    _write_private(output / "lae-platform-staging.env", _dotenv(environment))
    for name, content in manager_files.items():
        _write_private(output / name, content)
    # This artifact is installed as /opt/luma/control/control.env.  It is a
    # strict NAME=value data file consumed by Luma, not a sourceable shell
    # script; none of these values are inline credentials.
    _write_private(output / "lae-control.env", _dotenv(control_environment))

    fingerprints = {
        name: hashlib.sha256((content + "\n").encode()).hexdigest()
        for name, content in manager_files.items()
    }
    _write_private(
        output / "bundle-manifest.json",
        json.dumps(
            {
                "schemaVersion": "lae.staging-bundle/v1",
                "clusterId": args.cluster_id,
                "controlUrl": control_url,
                "publicApiUrl": public_api_url,
                "analyzerImageDigest": args.analyzer_image_digest,
                "llmBaseUrl": llm_base_url,
                "llmModel": args.llm_model.strip(),
                "managerFileSha256": fingerprints,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    return {
        "outputDir": str(output),
        "files": sorted(path.name for path in output.iterdir()),
    }


def main(argv: list[str] | None = None) -> int:
    try:
        result = generate(argv)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
