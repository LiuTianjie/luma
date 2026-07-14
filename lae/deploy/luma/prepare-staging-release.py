#!/usr/bin/env python3
"""Validate and update an existing private LAE staging bundle for a release.

This command deliberately preserves every credential.  It only changes the
cluster binding and immutable analyzer image, then refreshes the public bundle
manifest.  It never prints secret values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
from pathlib import Path


IMAGE_DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
CLUSTER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
EXPECTED_FILES = {
    "builder-agent-ai.env",
    "bundle-manifest.json",
    "credential-broker.token",
    "lae-admin.token",
    "lae-builder-principals.json",
    "lae-builder.token",
    "lae-control.env",
    "lae-plan-signing.json",
    "lae-platform-staging.env",
    "lae-runtime-principals.json",
    "lae-runtime.token",
    "object-broker.token",
}


def dotenv(path: Path, *, allow_legacy_export: bool = False) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if allow_legacy_export and line.startswith("export "):
            line = line.removeprefix("export ")
        if not line or "=" not in line:
            raise ValueError(f"{path.name} has an invalid line at {number}")
        name, value = line.split("=", 1)
        if allow_legacy_export and len(value) >= 2 and value[0] == value[-1] == "'":
            # Legacy operator bundles were sourceable shell fragments.  Only
            # literal single-quoted values were permitted there; normalize
            # them into the strict non-shell control.env representation.
            value = value[1:-1]
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values or not value:
            raise ValueError(f"{path.name} has an invalid entry at {number}")
        values[name] = value
    return values


def write_private(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def encode_dotenv(values: dict[str, str]) -> str:
    return "".join(f"{name}={values[name]}\n" for name in sorted(values))


def validate(bundle: Path, *, cluster_id: str, analyzer: str) -> dict[str, object]:
    metadata = bundle.lstat()
    if bundle.is_symlink() or not bundle.is_dir() or metadata.st_mode & 0o077:
        raise ValueError("bundle directory must be a private real directory")
    names = {path.name for path in bundle.iterdir()}
    if names != EXPECTED_FILES:
        raise ValueError("bundle file set is incomplete or contains unexpected files")
    for path in bundle.iterdir():
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_mode & 0o077:
            raise ValueError(f"bundle file is not private and regular: {path.name}")

    platform = dotenv(bundle / "lae-platform-staging.env")
    control = dotenv(bundle / "lae-control.env")
    builder_ai = dotenv(bundle / "builder-agent-ai.env")
    pairs = {
        "LAE_LUMA_SERVICE_TOKEN": "lae-builder.token",
        "LAE_LUMA_RUNTIME_SERVICE_TOKEN": "lae-runtime.token",
        "LAE_CREDENTIAL_BROKER_TOKEN": "credential-broker.token",
        "LAE_OBJECT_SOURCE_BROKER_TOKEN": "object-broker.token",
        "LAE_ADMIN_API_TOKEN": "lae-admin.token",
    }
    for name, filename in pairs.items():
        if platform.get(name) != (bundle / filename).read_text(encoding="utf-8").strip():
            raise ValueError(f"bundle credential binding is inconsistent: {name}")
    if builder_ai.get("LUMA_BUILDER_ANALYZE_CONTROLLER_TOKEN") != platform.get(
        "LAE_AGENT_CONTROLLER_TOKEN"
    ):
        raise ValueError("Builder controller credential binding is inconsistent")
    if platform.get("LAE_LUMA_CLUSTER_ID") != cluster_id:
        raise ValueError("bundle cluster binding does not match the live cluster")
    if platform.get("LAE_ANALYZER_IMAGE_DIGEST") != analyzer:
        raise ValueError("platform analyzer digest does not match the release")
    template_smoke_token = platform.get("LAE_TEMPLATE_SMOKE_REPORT_TOKEN", "")
    if not template_smoke_token.startswith("lae_template_smoke_") or not 32 <= len(
        template_smoke_token
    ) <= 512:
        raise ValueError("template smoke credential is missing or invalid")
    if control.get("LUMA_BUILDER_ANALYZE_IMAGE_DIGEST") != analyzer:
        raise ValueError("Control analyzer digest does not match the release")
    manifest = json.loads((bundle / "bundle-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("clusterId") != cluster_id or manifest.get("analyzerImageDigest") != analyzer:
        raise ValueError("bundle manifest does not match the release")
    return {
        "ok": True,
        "schemaVersion": "lae.staging-release-preflight/v1",
        "clusterId": cluster_id,
        "analyzerImageDigest": analyzer,
        "bundleFingerprint": hashlib.sha256(
            (bundle / "bundle-manifest.json").read_bytes()
        ).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-dir", required=True)
    parser.add_argument("--cluster-id", required=True)
    parser.add_argument("--analyzer-image-digest", required=True)
    parser.add_argument(
        "--agent-controller-url", default="https://lae-agent-staging.itool.tech"
    )
    parser.add_argument(
        "--runtime-storage-class", default="lae-staging-runtime-nfs"
    )
    parser.add_argument(
        "--runtime-node", action="append", dest="runtime_nodes", default=None
    )
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args(argv)
    if CLUSTER_ID.fullmatch(args.cluster_id) is None:
        raise ValueError("cluster id is invalid")
    if IMAGE_DIGEST.fullmatch(args.analyzer_image_digest) is None:
        raise ValueError("analyzer image must be an immutable repository digest")
    bundle = Path(args.bundle_dir).expanduser().resolve()
    if args.update:
        platform = dotenv(bundle / "lae-platform-staging.env")
        control = dotenv(bundle / "lae-control.env", allow_legacy_export=True)
        manifest = json.loads((bundle / "bundle-manifest.json").read_text(encoding="utf-8"))
        platform["LAE_LUMA_CLUSTER_ID"] = args.cluster_id
        platform["LAE_ANALYZER_IMAGE_DIGEST"] = args.analyzer_image_digest
        platform["LAE_ARTIFACT_INIT_IMAGE"] = "100.66.177.70:5000/liutianjie/luma/artifact-init@sha256:281024c94a47808a8e48cdfd02977fd5ef456820a44f5056c759b7c8c4a7afaf"
        platform["LAE_BACKUP_IMAGE"] = "100.66.177.70:5000/liutianjie/luma/backup@sha256:b8007b98a03da177c8bf84795aeffa70feface7f5ac963372cf4e1febef3d3df"
        platform["LAE_MINIO_IMAGE"] = "100.66.177.70:5000/liutianjie/luma/artifact-store@sha256:bcda2439046659f7f2900ec5937f2848fcc93d5cedcef88a86c74d70dc51daed"
        platform["LAE_VALKEY_IMAGE"] = "100.66.177.70:5000/liutianjie/luma/valkey@sha256:344564180c9f6ab456f1de502a88426f2360183d929c06514010c9200b3069db"
        control["LUMA_BUILDER_ANALYZE_IMAGE_DIGEST"] = args.analyzer_image_digest
        runtime_nodes = sorted(set(args.runtime_nodes or ["manager", "tecent"]))
        if not runtime_nodes or any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", node) is None
            for node in runtime_nodes
        ):
            raise ValueError("runtime node allowlist is invalid")
        if re.fullmatch(r"[a-z][a-z0-9-]{0,62}", args.runtime_storage_class) is None:
            raise ValueError("runtime storage class is invalid")
        control["LUMA_LAE_RUNTIME_STORAGE_CLASS"] = args.runtime_storage_class
        control["LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON"] = json.dumps(
            runtime_nodes, separators=(",", ":")
        )
        if not platform.get("LAE_AGENT_CONTROLLER_TOKEN"):
            # One-time migration for legacy bundles that relied on a separately
            # managed scoped secret.  The new value is written only to the
            # private bundle and is never printed.
            platform["LAE_AGENT_CONTROLLER_TOKEN"] = (
                "lae_agent_" + secrets.token_urlsafe(36)
            )
        if not platform.get("LAE_TEMPLATE_SMOKE_REPORT_TOKEN"):
            # Additive one-time migration. Existing application/data keys and
            # service principals remain byte-for-byte unchanged.
            platform["LAE_TEMPLATE_SMOKE_REPORT_TOKEN"] = (
                "lae_template_smoke_" + secrets.token_urlsafe(36)
            )
        manifest["clusterId"] = args.cluster_id
        manifest["analyzerImageDigest"] = args.analyzer_image_digest
        write_private(bundle / "lae-platform-staging.env", encode_dotenv(platform))
        write_private(bundle / "lae-control.env", encode_dotenv(control))
        if not (bundle / "builder-agent-ai.env").exists():
            write_private(
                bundle / "builder-agent-ai.env",
                encode_dotenv(
                    {
                        "LUMA_BUILDER_ANALYZE_AI_REQUIRED": "1",
                        "LUMA_BUILDER_ANALYZE_CONTROLLER_TOKEN": platform[
                            "LAE_AGENT_CONTROLLER_TOKEN"
                        ],
                        "LUMA_BUILDER_ANALYZE_CONTROLLER_URL": args.agent_controller_url.rstrip(
                            "/"
                        ),
                    }
                ),
            )
        write_private(
            bundle / "bundle-manifest.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        )
    print(json.dumps(validate(bundle, cluster_id=args.cluster_id, analyzer=args.analyzer_image_digest), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"prepare-staging-release: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
