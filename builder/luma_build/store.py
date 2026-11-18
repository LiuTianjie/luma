"""Standalone SQLite WAL store for luma-build.

Completely independent of Luma Control's database so luma-build can run on any
host or container and evolve on its own release cadence.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

from .crypto import SecretBox
from .errors import BuildError
from .registry_naming import (
    derive_repo_slug,
    is_http_registry,
    normalize_registry_host,
    normalize_repo_url,
    validate_git_ref,
)
from .scheduler import (
    DEFAULT_AFFINITY_WAIT_SECONDS,
    is_runner_eligible_for_lease,
    normalize_platform,
    parse_platforms,
    runner_is_online,
)

TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}
ACTIVE_STATUSES = {"queued", "building", "queued_deploy", "deploying"}
STALE_LEASE_SECONDS = 180


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class BuildStore:
    def __init__(
        self,
        data_dir: Path,
        *,
        default_registry_host: str = "",
        default_push_host: str = "",
        default_registry_namespace: str = "",
        default_registry_flatten: bool | None = None,
        affinity_wait_seconds: int = DEFAULT_AFFINITY_WAIT_SECONDS,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.data_dir.chmod(0o700)
        except OSError:
            pass
        self.db_path = self.data_dir / "luma-build.sqlite3"
        self.secret_box = SecretBox.from_env_or_file(self.data_dir)
        self.default_registry_host = default_registry_host.strip()
        self.default_push_host = (default_push_host or default_registry_host).strip()
        self.default_registry_namespace = default_registry_namespace.strip()
        self.default_registry_flatten = default_registry_flatten
        self.affinity_wait_seconds = affinity_wait_seconds
        self._write_lock = threading.RLock()
        self._init_schema()

    @contextlib.contextmanager
    def _conn(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write:
            self._write_lock.acquire()
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            if write:
                conn.execute("BEGIN IMMEDIATE;")
            yield conn
            if write and conn.in_transaction:
                conn.commit()
        except Exception:
            if write and conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
            if write:
                self._write_lock.release()

    def _init_schema(self) -> None:
        with self._conn(write=True) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runners (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL UNIQUE,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    native_platform TEXT NOT NULL DEFAULT '',
                    os_name TEXT NOT NULL DEFAULT '',
                    arch TEXT NOT NULL DEFAULT '',
                    version TEXT NOT NULL DEFAULT '',
                    max_concurrency INTEGER NOT NULL DEFAULT 1,
                    weight INTEGER NOT NULL DEFAULT 100,
                    cache_mode TEXT NOT NULL DEFAULT 'read-write',
                    labels_json TEXT NOT NULL DEFAULT '{}',
                    last_seen_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS git_credentials (
                    id TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    path_prefix TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT 'x-access-token',
                    encrypted_token TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS registry_credentials (
                    host TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    encrypted_password TEXT NOT NULL,
                    insecure INTEGER,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS builds (
                    id TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL,
                    repo_url TEXT NOT NULL,
                    repo_slug TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    deploy_enabled INTEGER NOT NULL DEFAULT 1,
                    runner_name TEXT NOT NULL DEFAULT '',
                    pinned_runner TEXT NOT NULL DEFAULT '',
                    platforms_json TEXT NOT NULL DEFAULT '["linux/amd64"]',
                    required_labels_json TEXT NOT NULL DEFAULT '{}',
                    request_json TEXT NOT NULL DEFAULT '{}',
                    encrypted_env_secrets TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    message TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    superseded_by TEXT NOT NULL DEFAULT '',
                    lease_heartbeat_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER NOT NULL DEFAULT 0,
                    built_at INTEGER NOT NULL DEFAULT 0,
                    completed_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_builds_status_seq ON builds(status, seq);
                CREATE INDEX IF NOT EXISTS idx_builds_repo_slug ON builds(repo_slug, created_at DESC);

                CREATE TABLE IF NOT EXISTS build_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    build_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(build_id) REFERENCES builds(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_build_events_build_seq ON build_events(build_id, seq);

                CREATE TABLE IF NOT EXISTS build_affinity (
                    project_key TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    runner_name TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(project_key, platform)
                );

                CREATE TABLE IF NOT EXISTS counters (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                """
            )
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def _next_seq(self, conn: sqlite3.Connection, counter_name: str = "build_seq") -> int:
        row = conn.execute("SELECT value FROM counters WHERE name = ?", (counter_name,)).fetchone()
        nxt = (int(row["value"]) if row else 0) + 1
        conn.execute(
            "INSERT INTO counters(name, value) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (counter_name, nxt),
        )
        return nxt

    # ------------------------------------------------------------------
    # Runner registration & management
    # ------------------------------------------------------------------

    def create_runner_token(
        self,
        *,
        name: str,
        weight: int = 100,
        max_concurrency: int = 1,
        cache_mode: str = "read-write",
        labels: dict[str, str] | None = None,
        rotate: bool = False,
    ) -> dict[str, Any]:
        clean_name = str(name or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", clean_name):
            raise BuildError("runner name must match ^[a-z0-9][a-z0-9._-]{0,63}$")
        if cache_mode not in {"read-write", "read-only", "local-only"}:
            raise BuildError("cacheMode must be read-write, read-only, or local-only")
        max_concurrency = max(1, min(int(max_concurrency), 16))
        weight = max(1, min(int(weight), 1000))
        raw_token = f"lbr_{secrets.token_urlsafe(32)}"
        token_hash = _sha256_hex(raw_token)
        now = int(time.time())
        labels_text = json.dumps(labels or {}, sort_keys=True)

        with self._conn(write=True) as conn:
            existing = conn.execute("SELECT id, revoked FROM runners WHERE name = ?", (clean_name,)).fetchone()
            if existing and not rotate and not existing["revoked"]:
                raise BuildError(
                    f"runner {clean_name!r} already exists; pass rotate=true to rotate its token"
                )
            if existing:
                runner_id = str(existing["id"])
                conn.execute(
                    """
                    UPDATE runners
                    SET token_hash = ?, revoked = 0, weight = ?, max_concurrency = ?,
                        cache_mode = ?, labels_json = ?
                    WHERE id = ?
                    """,
                    (token_hash, weight, max_concurrency, cache_mode, labels_text, runner_id),
                )
            else:
                runner_id = f"rn-{secrets.token_hex(6)}"
                conn.execute(
                    """
                    INSERT INTO runners(
                        id, name, token_hash, revoked, max_concurrency, weight,
                        cache_mode, labels_json, created_at
                    ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)
                    """,
                    (runner_id, clean_name, token_hash, max_concurrency, weight, cache_mode, labels_text, now),
                )
        return {
            "id": runner_id,
            "name": clean_name,
            "runnerToken": raw_token,
            "weight": weight,
            "maxConcurrency": max_concurrency,
            "cacheMode": cache_mode,
            "labels": labels or {},
        }

    def revoke_runner(self, name_or_id: str) -> dict[str, Any]:
        target = str(name_or_id or "").strip()
        with self._conn(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM runners WHERE name = ? OR id = ?", (target, target)
            ).fetchone()
            if not row:
                raise BuildError(f"runner not found: {target}", status_code=404)
            conn.execute("UPDATE runners SET revoked = 1 WHERE id = ?", (row["id"],))
            updated = conn.execute("SELECT * FROM runners WHERE id = ?", (row["id"],)).fetchone()
            return self._runner_public(updated, now=int(time.time()), active_slots=0)

    def list_runners(self, *, now: int | None = None) -> list[dict[str, Any]]:
        current = int(time.time() if now is None else now)
        with self._conn(write=False) as conn:
            rows = conn.execute("SELECT * FROM runners ORDER BY weight DESC, name ASC").fetchall()
            active = self._active_runner_counts(conn)
            return [
                self._runner_public(row, now=current, active_slots=active.get(str(row["name"]), 0))
                for row in rows
            ]

    def _runner_public(self, row: sqlite3.Row, *, now: int, active_slots: int) -> dict[str, Any]:
        record = {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "revoked": bool(row["revoked"]),
            "nativePlatform": str(row["native_platform"] or ""),
            "os": str(row["os_name"] or ""),
            "arch": str(row["arch"] or ""),
            "version": str(row["version"] or ""),
            "maxConcurrency": int(row["max_concurrency"] or 1),
            "activeBuilds": active_slots,
            "weight": int(row["weight"] or 100),
            "cacheMode": str(row["cache_mode"] or "read-write"),
            "labels": json.loads(row["labels_json"] or "{}"),
            "lastSeenAt": int(row["last_seen_at"] or 0),
            "createdAt": int(row["created_at"] or 0),
        }
        record["online"] = runner_is_online(record, now=now)
        return record

    def _authenticate_runner(self, conn: sqlite3.Connection, token: str) -> sqlite3.Row:
        clean = str(token or "").strip()
        if not clean.startswith("lbr_"):
            raise BuildError("unauthorized runner token", status_code=401)
        row = conn.execute(
            "SELECT * FROM runners WHERE token_hash = ?", (_sha256_hex(clean),)
        ).fetchone()
        if not row or row["revoked"]:
            raise BuildError("unauthorized or revoked runner token", status_code=401)
        return row

    def _active_runner_counts(self, conn: sqlite3.Connection) -> dict[str, int]:
        rows = conn.execute(
            "SELECT runner_name, COUNT(*) AS cnt FROM builds WHERE status = 'building' GROUP BY runner_name"
        ).fetchall()
        return {str(r["runner_name"]): int(r["cnt"]) for r in rows if r["runner_name"]}

    # ------------------------------------------------------------------
    # Git & Registry credentials (encrypted at rest)
    # ------------------------------------------------------------------

    def set_git_credential(
        self,
        *,
        cred_id: str,
        host: str,
        token: str,
        username: str = "x-access-token",
        path_prefix: str = "",
    ) -> dict[str, Any]:
        cid = str(cred_id or "").strip()
        clean_host = str(host or "").strip().lower()
        if "://" in clean_host:
            clean_host = urllib.parse.urlsplit(clean_host).netloc.lower()
        clean_prefix = str(path_prefix or "").strip().strip("/").lower()
        if not cid or not clean_host or not token:
            raise BuildError("id, host, and token are required for a Git credential")
        now = int(time.time())
        enc = self.secret_box.encrypt(token)
        with self._conn(write=True) as conn:
            conn.execute(
                """
                INSERT INTO git_credentials(id, host, path_prefix, username, encrypted_token, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    host = excluded.host,
                    path_prefix = excluded.path_prefix,
                    username = excluded.username,
                    encrypted_token = excluded.encrypted_token,
                    updated_at = excluded.updated_at
                """,
                (cid, clean_host, clean_prefix, username.strip() or "x-access-token", enc, now),
            )
        return {"id": cid, "host": clean_host, "pathPrefix": clean_prefix, "username": username, "updatedAt": now}

    def list_git_credentials(self) -> list[dict[str, Any]]:
        with self._conn(write=False) as conn:
            rows = conn.execute("SELECT id, host, path_prefix, username, updated_at FROM git_credentials ORDER BY id").fetchall()
            return [
                {
                    "id": str(r["id"]),
                    "host": str(r["host"]),
                    "pathPrefix": str(r["path_prefix"]),
                    "username": str(r["username"]),
                    "updatedAt": int(r["updated_at"]),
                }
                for r in rows
            ]

    def delete_git_credential(self, cred_id: str) -> bool:
        with self._conn(write=True) as conn:
            cur = conn.execute("DELETE FROM git_credentials WHERE id = ?", (cred_id.strip(),))
            return cur.rowcount > 0

    def _resolve_git_auth(
        self, conn: sqlite3.Connection, *, repo_url: str, provider_id: str = ""
    ) -> dict[str, str] | None:
        if provider_id:
            row = conn.execute(
                "SELECT username, encrypted_token FROM git_credentials WHERE id = ?",
                (provider_id,),
            ).fetchone()
            if row:
                return {
                    "username": str(row["username"]),
                    "token": self.secret_box.decrypt(str(row["encrypted_token"])),
                }
            env_match = self._env_git_auth_by_id(provider_id)
            if env_match:
                return env_match
            raise BuildError(f"unknown Git credential id: {provider_id}")

        parsed = urllib.parse.urlsplit(repo_url)
        host = (parsed.netloc or "").lower()
        repo_path = parsed.path.strip("/").lower()
        if not host:
            return None
        rows = conn.execute(
            "SELECT path_prefix, username, encrypted_token FROM git_credentials WHERE host = ?",
            (host,),
        ).fetchall()
        best: sqlite3.Row | None = None
        best_len = -1
        for row in rows:
            prefix = str(row["path_prefix"] or "")
            if not prefix or repo_path == prefix or repo_path.startswith(prefix + "/"):
                if len(prefix) > best_len:
                    best = row
                    best_len = len(prefix)
        if best:
            return {
                "username": str(best["username"]),
                "token": self.secret_box.decrypt(str(best["encrypted_token"])),
            }
        return self._env_git_auth_by_host(host)

    @staticmethod
    def _env_git_auth_by_id(provider_id: str) -> dict[str, str] | None:
        pid = provider_id.strip().lower()
        if pid.startswith("github") and os.environ.get("GITHUB_TOKEN", "").strip():
            return {
                "username": os.environ.get("GITHUB_USERNAME", "").strip() or "x-access-token",
                "token": os.environ["GITHUB_TOKEN"].strip(),
            }
        if pid.startswith("gitea") and os.environ.get("GITEA_TOKEN", "").strip():
            return {
                "username": os.environ.get("GITEA_USERNAME", "").strip() or "x-access-token",
                "token": os.environ["GITEA_TOKEN"].strip(),
            }
        return None

    @staticmethod
    def _env_git_auth_by_host(host: str) -> dict[str, str] | None:
        """Fallback to Luma scoped service secrets injected via container env."""
        clean_host = host.strip().lower()
        if clean_host == "github.com" and os.environ.get("GITHUB_TOKEN", "").strip():
            return {
                "username": os.environ.get("GITHUB_USERNAME", "").strip() or "x-access-token",
                "token": os.environ["GITHUB_TOKEN"].strip(),
            }
        gitea_raw = os.environ.get("GITEA_HOST", "").strip().lower()
        if "://" in gitea_raw:
            gitea_raw = urllib.parse.urlsplit(gitea_raw).netloc.lower()
        if gitea_raw and clean_host == gitea_raw and os.environ.get("GITEA_TOKEN", "").strip():
            return {
                "username": os.environ.get("GITEA_USERNAME", "").strip() or "x-access-token",
                "token": os.environ["GITEA_TOKEN"].strip(),
            }
        return None

    def set_registry_credential(
        self,
        *,
        host: str,
        username: str,
        password: str,
        insecure: bool | None = None,
    ) -> dict[str, Any]:
        norm_host = normalize_registry_host(host)
        if not username.strip() or not password:
            raise BuildError("registry username and password are required")
        now = int(time.time())
        enc = self.secret_box.encrypt(password)
        ins_val = None if insecure is None else int(bool(insecure))
        with self._conn(write=True) as conn:
            conn.execute(
                """
                INSERT INTO registry_credentials(host, username, encrypted_password, insecure, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(host) DO UPDATE SET
                    username = excluded.username,
                    encrypted_password = excluded.encrypted_password,
                    insecure = excluded.insecure,
                    updated_at = excluded.updated_at
                """,
                (norm_host, username.strip(), enc, ins_val, now),
            )
        return {
            "host": norm_host,
            "username": username.strip(),
            "insecure": is_http_registry(norm_host, insecure),
            "updatedAt": now,
        }

    def list_registry_credentials(self) -> list[dict[str, Any]]:
        with self._conn(write=False) as conn:
            rows = conn.execute(
                "SELECT host, username, insecure, updated_at FROM registry_credentials ORDER BY host"
            ).fetchall()
            return [
                {
                    "host": str(r["host"]),
                    "username": str(r["username"]),
                    "insecure": is_http_registry(
                        str(r["host"]), None if r["insecure"] is None else bool(r["insecure"])
                    ),
                    "updatedAt": int(r["updated_at"]),
                }
                for r in rows
            ]

    def delete_registry_credential(self, host: str) -> bool:
        norm = normalize_registry_host(host)
        with self._conn(write=True) as conn:
            cur = conn.execute("DELETE FROM registry_credentials WHERE host = ?", (norm,))
            return cur.rowcount > 0

    def _resolve_registry_auths(
        self, conn: sqlite3.Connection, hosts: list[str]
    ) -> tuple[list[dict[str, str]], dict[str, bool]]:
        auths: list[dict[str, str]] = []
        insecure_map: dict[str, bool] = {}
        seen: set[str] = set()
        env_reg_user = os.environ.get("LUMA_BUILD_REGISTRY_USERNAME", "").strip()
        env_reg_pass = os.environ.get("LUMA_BUILD_REGISTRY_PASSWORD", "").strip()
        default_hosts = {
            normalize_registry_host(h)
            for h in (self.default_registry_host, self.default_push_host)
            if h
        }
        for raw in hosts:
            if not raw:
                continue
            norm = normalize_registry_host(raw)
            if norm in seen:
                continue
            seen.add(norm)
            row = conn.execute(
                "SELECT username, encrypted_password, insecure FROM registry_credentials WHERE host = ?",
                (norm,),
            ).fetchone()
            explicit_insecure = None if (row is None or row["insecure"] is None) else bool(row["insecure"])
            insecure_map[norm] = is_http_registry(norm, explicit_insecure)
            if row:
                auths.append(
                    {
                        "host": norm,
                        "username": str(row["username"]),
                        "password": self.secret_box.decrypt(str(row["encrypted_password"])),
                    }
                )
            elif norm in default_hosts and env_reg_user and env_reg_pass:
                # Directly consume Luma scoped secrets injected into the luma-build service!
                auths.append(
                    {
                        "host": norm,
                        "username": env_reg_user,
                        "password": env_reg_pass,
                    }
                )
        return auths, insecure_map

    # ------------------------------------------------------------------
    # Build queue, lease, progress, completion & deployment stage
    # ------------------------------------------------------------------

    def enqueue_build(self, body: dict[str, Any]) -> dict[str, Any]:
        provider_id = str(body.get("providerId") or "").strip()
        default_git_host = "github.com"
        if provider_id:
            with self._conn(write=False) as conn:
                prow = conn.execute(
                    "SELECT host FROM git_credentials WHERE id = ?", (provider_id,)
                ).fetchone()
            if prow:
                default_git_host = str(prow["host"])
            elif self._env_git_auth_by_id(provider_id):
                if provider_id.lower().startswith("gitea") and os.environ.get("GITEA_HOST", "").strip():
                    gh = os.environ["GITEA_HOST"].strip()
                    default_git_host = urllib.parse.urlsplit(gh).netloc if "://" in gh else gh
                else:
                    default_git_host = "github.com"
            else:
                raise BuildError(f"unknown Git credential providerId: {provider_id}")

        repo_url = normalize_repo_url(
            str(body.get("repoUrl") or ""),
            repository=str(body.get("repository") or ""),
            default_host=default_git_host,
        )
        repo_slug = derive_repo_slug(repo_url)
        ref = validate_git_ref(str(body.get("ref") or "main"))

        registry_host = normalize_registry_host(
            str(body.get("registryHost") or self.default_registry_host or "")
        )
        push_host = normalize_registry_host(
            str(body.get("pushHost") or self.default_push_host or registry_host)
        )
        namespace = str(
            body["registryNamespace"]
            if "registryNamespace" in body
            else self.default_registry_namespace
        ).strip()
        flatten = (
            bool(body["registryFlatten"])
            if "registryFlatten" in body and body["registryFlatten"] is not None
            else self.default_registry_flatten
        )

        default_plat = os.environ.get("LUMA_BUILD_DEFAULT_PLATFORM", "").strip() or "linux/amd64"
        platforms = parse_platforms(body.get("platforms") or body.get("platform") or default_plat)
        pinned_runner = str(body.get("runner") or body.get("buildNode") or "").strip().lower()
        deploy_enabled = bool(body.get("deploy", True))
        compose_sidecar = str(body.get("composeSidecar") or "").strip()
        context_rel = str(body.get("context") or "").strip()
        dockerfile_rel = str(body.get("dockerfile") or "").strip()
        target_stage = str(body.get("target") or "").strip()
        for label, rel_val in (
            ("composeSidecar", compose_sidecar),
            ("context", context_rel),
            ("dockerfile", dockerfile_rel),
        ):
            if rel_val.startswith("-") or "\x00" in rel_val:
                raise BuildError(f"invalid {label}: {rel_val!r}")
        if target_stage and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", target_stage):
            raise BuildError(f"invalid Dockerfile target stage: {target_stage!r}")

        raw_build_args = body.get("buildArgs") if isinstance(body.get("buildArgs"), dict) else {}
        clean_build_args: dict[str, str] = {}
        for k, v in raw_build_args.items():
            key_str = str(k).strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key_str):
                raise BuildError(f"invalid build arg name: {k!r}")
            clean_build_args[key_str] = str(v)

        explicit_app = str(body.get("appName") or "").strip().lower()
        if explicit_app:
            target_key = f"app:{explicit_app}"
        elif compose_sidecar:
            target_key = f"compose:{compose_sidecar}"
        else:
            target_key = json.dumps(
                ["service", context_rel or ".", dockerfile_rel or "Dockerfile", target_stage],
                separators=(",", ":"),
            )
        supersede = bool(body.get("supersede", True))

        env_secrets = body.get("envSecrets")
        enc_env = ""
        if isinstance(env_secrets, dict) and env_secrets:
            clean_secrets = {str(k): str(v) for k, v in env_secrets.items() if str(k).strip()}
            enc_env = self.secret_box.encrypt(json.dumps(clean_secrets, sort_keys=True))

        req_labels = body.get("requiredLabels") if isinstance(body.get("requiredLabels"), dict) else {}
        stored_request = {
            "repoUrl": repo_url,
            "repoSlug": repo_slug,
            "ref": ref,
            "providerId": provider_id,
            "registryHost": registry_host,
            "pushHost": push_host,
            "registryNamespace": namespace,
            "registryFlatten": flatten,
            "platforms": platforms,
            "context": context_rel,
            "dockerfile": dockerfile_rel,
            "target": target_stage,
            "composeSidecar": compose_sidecar,
            "manifest": str(body.get("manifest") or ""),
            "region": str(body.get("region") or "").strip(),
            "node": str(body.get("node") or "").strip(),
            "exposure": str(body.get("exposure") or "").strip(),
            "domain": str(body.get("domain") or "").strip(),
            "port": body.get("port"),
            "buildArgs": clean_build_args,
            "noCache": bool(body.get("noCache", False)),
            "buildTimeout": min(max(int(body.get("buildTimeout") or 7200), 60), 14400),
            "deploy": deploy_enabled,
            "envSecretNames": sorted(str(k) for k in (env_secrets or {}).keys()) if isinstance(env_secrets, dict) else [],
        }

        now = int(time.time())
        build_id = f"bld-{int(now * 1000)}-{secrets.token_hex(4)}"

        with self._conn(write=True) as conn:
            if pinned_runner:
                rrow = conn.execute(
                    "SELECT revoked FROM runners WHERE name = ?", (pinned_runner,)
                ).fetchone()
                if not rrow or rrow["revoked"]:
                    raise BuildError(f"pinned runner is not registered or is revoked: {pinned_runner}")

            seq = self._next_seq(conn, "build_seq")
            if supersede:
                older_rows = conn.execute(
                    """
                    SELECT id, status FROM builds
                    WHERE repo_slug = ? AND ref = ? AND target_key = ?
                      AND status IN ('queued', 'building')
                    """,
                    (repo_slug, ref, target_key),
                ).fetchall()
                for older in older_rows:
                    oid = str(older["id"])
                    if older["status"] == "queued":
                        conn.execute(
                            """
                            UPDATE builds
                            SET status = 'canceled', superseded_by = ?, completed_at = ?, updated_at = ?,
                                message = ?
                            WHERE id = ?
                            """,
                            (build_id, now, now, f"Superseded by {build_id}", oid),
                        )
                    elif older["status"] == "building":
                        conn.execute(
                            """
                            UPDATE builds
                            SET cancel_requested = 1, superseded_by = ?, updated_at = ?,
                                message = ?
                            WHERE id = ?
                            """,
                            (build_id, now, f"Canceling (superseded by {build_id})", oid),
                        )

            conn.execute(
                """
                INSERT INTO builds(
                    id, seq, repo_url, repo_slug, ref, target_key, status,
                    deploy_enabled, pinned_runner, platforms_json, required_labels_json,
                    request_json, encrypted_env_secrets, message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    build_id,
                    seq,
                    repo_url,
                    repo_slug,
                    ref,
                    target_key,
                    int(deploy_enabled),
                    pinned_runner,
                    json.dumps(platforms),
                    json.dumps(req_labels, sort_keys=True),
                    json.dumps(stored_request, sort_keys=True),
                    enc_env,
                    "Queued for compatible runner",
                    now,
                    now,
                ),
            )
            self._insert_event(conn, build_id, "queue", "status", f"Queued {repo_slug}@{ref} ({','.join(platforms)})", now)
            self._prune_completed_builds(conn, keep=500)
            row = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
            return self._build_public(row)

    def _prune_completed_builds(self, conn: sqlite3.Connection, *, keep: int = 500) -> None:
        conn.execute(
            """
            DELETE FROM builds WHERE id IN (
                SELECT id FROM builds
                WHERE status IN ('succeeded', 'failed', 'canceled')
                ORDER BY seq DESC LIMIT -1 OFFSET ?
            )
            """,
            (max(int(keep), 50),),
        )

    def recover_interrupted_deploys(self, *, now: int | None = None) -> int:
        """Mark deployments interrupted by a luma-build restart as failed so targets unblock."""
        current = int(time.time() if now is None else now)
        with self._conn(write=True) as conn:
            rows = conn.execute("SELECT id FROM builds WHERE status = 'deploying'").fetchall()
            for row in rows:
                bid = str(row["id"])
                msg = "Deployment interrupted by luma-build restart; inspect Luma Control before retrying"
                conn.execute(
                    """
                    UPDATE builds
                    SET status = 'failed', message = ?, encrypted_env_secrets = '',
                        completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (msg, current, current, bid),
                )
                self._insert_event(conn, bid, "deploy", "error", msg, current)
            return len(rows)

    def _insert_event(
        self, conn: sqlite3.Connection, build_id: str, stage: str, kind: str, message: str, now: int
    ) -> None:
        last = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM build_events WHERE build_id = ?", (build_id,)
        ).fetchone()
        seq = int(last["m"]) + 1
        conn.execute(
            "INSERT INTO build_events(build_id, seq, stage, kind, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (build_id, seq, stage, kind, str(message)[:4000], now),
        )

    def append_events(self, build_id: str, stage: str, lines: list[dict[str, Any]]) -> None:
        if not lines:
            return
        now = int(time.time())
        with self._conn(write=True) as conn:
            last = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM build_events WHERE build_id = ?", (build_id,)
            ).fetchone()
            seq = int(last["m"])
            for item in lines:
                seq += 1
                kind = str(item.get("kind") or "log")
                msg = str(item.get("message") or item.get("line") or "")[:4000]
                ts = int(item.get("ts") or now)
                conn.execute(
                    "INSERT INTO build_events(build_id, seq, stage, kind, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (build_id, seq, stage, kind, msg, ts),
                )
            conn.execute("UPDATE builds SET updated_at = ? WHERE id = ?", (now, build_id))

    def reap_stale_builds(self, *, now: int | None = None) -> int:
        current = int(time.time() if now is None else now)
        cutoff = current - STALE_LEASE_SECONDS
        with self._conn(write=True) as conn:
            stale = conn.execute(
                "SELECT id, runner_name FROM builds WHERE status = 'building' AND lease_heartbeat_at > 0 AND lease_heartbeat_at < ?",
                (cutoff,),
            ).fetchall()
            for row in stale:
                bid = str(row["id"])
                msg = f"Runner {row['runner_name']} stopped reporting heartbeats; marking build failed"
                conn.execute(
                    """
                    UPDATE builds
                    SET status = 'failed', message = ?, completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (msg, current, current, bid),
                )
                self._insert_event(conn, bid, "build", "error", msg, current)
            return len(stale)

    def poll_and_lease(self, token: str, runner_info: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        """Update runner heartbeat and atomically claim the best eligible queued build."""
        current = int(time.time() if now is None else now)
        native_platform = normalize_platform(str(runner_info.get("nativePlatform") or ""))
        os_name = str(runner_info.get("os") or "").strip().lower()
        arch = str(runner_info.get("arch") or "").strip().lower()
        version = str(runner_info.get("version") or "").strip()
        cache_mode_override = str(runner_info.get("cacheMode") or "").strip()

        with self._conn(write=True) as conn:
            runner_row = self._authenticate_runner(conn, token)
            runner_id = str(runner_row["id"])
            runner_name = str(runner_row["name"])
            effective_cache_mode = (
                cache_mode_override
                if cache_mode_override in {"read-write", "read-only", "local-only"}
                else str(runner_row["cache_mode"] or "read-write")
            )
            conn.execute(
                """
                UPDATE runners
                SET native_platform = ?, os_name = ?, arch = ?, version = ?,
                    cache_mode = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (native_platform, os_name, arch, version, effective_cache_mode, current, runner_id),
            )
            runner_row = conn.execute("SELECT * FROM runners WHERE id = ?", (runner_id,)).fetchone()
            all_runner_rows = conn.execute("SELECT * FROM runners WHERE revoked = 0").fetchall()
            active_counts = self._active_runner_counts(conn)
            all_runners = [
                self._runner_public(r, now=current, active_slots=active_counts.get(str(r["name"]), 0))
                for r in all_runner_rows
            ]
            me = next(r for r in all_runners if r["name"] == runner_name)

            queued_rows = conn.execute(
                """
                SELECT * FROM builds
                WHERE status = 'queued'
                ORDER BY CASE WHEN pinned_runner = ? THEN 0 ELSE 1 END ASC, seq ASC
                LIMIT 50
                """,
                (runner_name,),
            ).fetchall()

            for brow in queued_rows:
                build_dict = self._build_public(brow)
                primary_platform = (build_dict["platforms"] or ["linux/amd64"])[0]
                aff_row = conn.execute(
                    "SELECT runner_name FROM build_affinity WHERE project_key = ? AND platform = ?",
                    (build_dict["repoSlug"], primary_platform),
                ).fetchone()
                warm_runner = str(aff_row["runner_name"]) if aff_row else ""

                if not is_runner_eligible_for_lease(
                    polling_runner=me,
                    all_runners=all_runners,
                    active_counts=active_counts,
                    build=build_dict,
                    warm_runner_name=warm_runner,
                    now=current,
                    affinity_wait_seconds=self.affinity_wait_seconds,
                ):
                    continue

                req = json.loads(brow["request_json"] or "{}")
                git_auth = self._resolve_git_auth(
                    conn, repo_url=req["repoUrl"], provider_id=str(req.get("providerId") or "")
                )
                reg_auths, insecure_map = self._resolve_registry_auths(
                    conn, [req["pushHost"], req["registryHost"]]
                )

                conn.execute(
                    """
                    UPDATE builds
                    SET status = 'building', runner_name = ?, started_at = ?,
                        lease_heartbeat_at = ?, updated_at = ?, message = ?
                    WHERE id = ?
                    """,
                    (
                        runner_name,
                        current,
                        current,
                        current,
                        f"Building on runner {runner_name} ({native_platform})",
                        build_dict["id"],
                    ),
                )
                self._insert_event(
                    conn,
                    build_dict["id"],
                    "build",
                    "status",
                    f"Leased by runner {runner_name} ({native_platform}, cache={effective_cache_mode})",
                    current,
                )

                task_payload = {
                    **req,
                    "buildId": build_dict["id"],
                    "runnerName": runner_name,
                    "cacheMode": effective_cache_mode,
                    "pushInsecure": insecure_map.get(normalize_registry_host(req["pushHost"]), False),
                    "pullInsecure": insecure_map.get(normalize_registry_host(req["registryHost"]), False),
                    "gitAuth": git_auth,
                    "registryAuths": reg_auths,
                }
                return {"runner": me, "task": task_payload}

            return {"runner": me, "task": None}

    def runner_heartbeat(self, token: str, *, build_id: str = "", now: int | None = None) -> dict[str, Any]:
        current = int(time.time() if now is None else now)
        with self._conn(write=True) as conn:
            rrow = self._authenticate_runner(conn, token)
            rname = str(rrow["name"])
            conn.execute("UPDATE runners SET last_seen_at = ? WHERE id = ?", (current, rrow["id"]))
            cancel_requested = False
            if build_id:
                brow = conn.execute(
                    "SELECT status, runner_name, cancel_requested FROM builds WHERE id = ?",
                    (build_id,),
                ).fetchone()
                if brow and str(brow["runner_name"]) == rname:
                    conn.execute(
                        "UPDATE builds SET lease_heartbeat_at = ?, updated_at = ? WHERE id = ?",
                        (current, current, build_id),
                    )
                    cancel_requested = bool(brow["cancel_requested"]) or brow["status"] == "canceled"
            return {"runner": rname, "cancelRequested": cancel_requested}

    def runner_progress(self, token: str, *, build_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        now = int(time.time())
        with self._conn(write=True) as conn:
            rrow = self._authenticate_runner(conn, token)
            rname = str(rrow["name"])
            conn.execute("UPDATE runners SET last_seen_at = ? WHERE id = ?", (now, rrow["id"]))
            brow = conn.execute(
                "SELECT runner_name, cancel_requested, status FROM builds WHERE id = ?", (build_id,)
            ).fetchone()
            if not brow or str(brow["runner_name"]) != rname:
                raise BuildError(f"build {build_id} is not owned by runner {rname}", status_code=409)
            conn.execute(
                "UPDATE builds SET lease_heartbeat_at = ?, updated_at = ? WHERE id = ?",
                (now, now, build_id),
            )
            last = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM build_events WHERE build_id = ?", (build_id,)
            ).fetchone()
            seq = int(last["m"])
            for item in events[:200]:
                seq += 1
                kind = str(item.get("kind") or "log")
                msg = str(item.get("message") or item.get("line") or "")[:4000]
                conn.execute(
                    "INSERT INTO build_events(build_id, seq, stage, kind, message, created_at) VALUES (?, ?, 'build', ?, ?, ?)",
                    (build_id, seq, kind, msg, now),
                )
            return {"accepted": len(events[:200]), "cancelRequested": bool(brow["cancel_requested"])}

    def runner_complete(
        self,
        token: str,
        *,
        build_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        message: str = "",
        now: int | None = None,
    ) -> dict[str, Any]:
        if status not in {"succeeded", "failed", "canceled"}:
            raise BuildError("runner completion status must be succeeded, failed, or canceled")
        current = int(time.time() if now is None else now)
        with self._conn(write=True) as conn:
            rrow = self._authenticate_runner(conn, token)
            rname = str(rrow["name"])
            conn.execute("UPDATE runners SET last_seen_at = ? WHERE id = ?", (current, rrow["id"]))
            brow = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
            if not brow or str(brow["runner_name"]) != rname:
                raise BuildError(f"build {build_id} is not owned by runner {rname}", status_code=409)

            # Idempotent replay support if network dropped the first ACK
            if str(brow["status"]) in TERMINAL_STATUSES | {"queued_deploy", "deploying"}:
                return {"build": self._build_public(brow), "replayed": True}

            res = result if isinstance(result, dict) else {}
            if status == "succeeded" and not brow["cancel_requested"]:
                # Record BuildKit cache affinity for this repo + architecture!
                platforms = json.loads(brow["platforms_json"] or '["linux/amd64"]')
                primary_platform = (platforms or ["linux/amd64"])[0]
                conn.execute(
                    """
                    INSERT INTO build_affinity(project_key, platform, runner_name, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(project_key, platform) DO UPDATE SET
                        runner_name = excluded.runner_name,
                        updated_at = excluded.updated_at
                    """,
                    (str(brow["repo_slug"]), primary_platform, rname, current),
                )

                if brow["deploy_enabled"]:
                    next_status = "queued_deploy"
                    next_msg = f"Image pushed by {rname}; queued for deployment to Luma Control"
                    completed_at = 0
                else:
                    next_status = "succeeded"
                    next_msg = message or f"Image built and pushed by {rname}"
                    completed_at = current
            else:
                next_status = "canceled" if (status == "canceled" or brow["cancel_requested"]) else "failed"
                next_msg = message or ("Build canceled" if next_status == "canceled" else "Build failed")
                completed_at = current

            conn.execute(
                """
                UPDATE builds
                SET status = ?, result_json = ?, message = ?, built_at = ?,
                    completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_status, json.dumps(res, sort_keys=True), next_msg[:2000], current, completed_at, current, build_id),
            )
            self._insert_event(
                conn,
                build_id,
                "build",
                "error" if next_status == "failed" else "status",
                next_msg,
                current,
            )
            updated = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
            return {"build": self._build_public(updated), "replayed": False}

    # ------------------------------------------------------------------
    # Deployment handoff stage (Runner is already free!)
    # ------------------------------------------------------------------

    def claim_next_deploy(self) -> tuple[dict[str, Any], dict[str, str]] | None:
        now = int(time.time())
        with self._conn(write=True) as conn:
            # Serialize deployments per (repo_slug, target_key) so two commits of the
            # same app never race on Luma Control simultaneously.
            busy_targets = {
                (str(r["repo_slug"]), str(r["target_key"]))
                for r in conn.execute(
                    "SELECT repo_slug, target_key FROM builds WHERE status = 'deploying'"
                ).fetchall()
            }
            candidates = conn.execute(
                "SELECT * FROM builds WHERE status = 'queued_deploy' ORDER BY seq ASC"
            ).fetchall()
            for row in candidates:
                key = (str(row["repo_slug"]), str(row["target_key"]))
                if key in busy_targets:
                    continue
                bid = str(row["id"])
                if row["cancel_requested"]:
                    conn.execute(
                        "UPDATE builds SET status = 'canceled', completed_at = ?, updated_at = ?, message = 'Canceled before deploy' WHERE id = ?",
                        (now, now, bid),
                    )
                    continue
                # Check if a newer build of the exact same branch + target already deployed or is queued_deploy ahead
                newer = conn.execute(
                    """
                    SELECT id FROM builds
                    WHERE repo_slug = ? AND ref = ? AND target_key = ?
                      AND seq > ? AND status IN ('queued_deploy', 'deploying', 'succeeded')
                    ORDER BY seq DESC LIMIT 1
                    """,
                    (row["repo_slug"], row["ref"], row["target_key"], row["seq"]),
                ).fetchone()
                if newer:
                    nid = str(newer["id"])
                    conn.execute(
                        """
                        UPDATE builds
                        SET status = 'canceled', superseded_by = ?, completed_at = ?, updated_at = ?,
                            message = ?
                        WHERE id = ?
                        """,
                        (nid, now, now, f"Skipped deploy (superseded by newer build {nid})", bid),
                    )
                    self._insert_event(conn, bid, "deploy", "status", f"Skipped deploy (superseded by {nid})", now)
                    continue

                conn.execute(
                    "UPDATE builds SET status = 'deploying', updated_at = ?, message = 'Deploying to Luma Control' WHERE id = ?",
                    (now, bid),
                )
                self._insert_event(conn, bid, "deploy", "status", "Submitting immutable image to Luma Control", now)
                env_secrets: dict[str, str] = {}
                if row["encrypted_env_secrets"]:
                    env_secrets = json.loads(self.secret_box.decrypt(str(row["encrypted_env_secrets"])))
                updated = conn.execute("SELECT * FROM builds WHERE id = ?", (bid,)).fetchone()
                return self._build_internal(updated), env_secrets
        return None

    def complete_deploy(
        self, build_id: str, *, succeeded: bool, deploy_result: dict[str, Any] | None = None, message: str = ""
    ) -> dict[str, Any]:
        now = int(time.time())
        with self._conn(write=True) as conn:
            row = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
            if not row:
                raise BuildError(f"build not found: {build_id}", status_code=404)
            merged_result = json.loads(row["result_json"] or "{}")
            if deploy_result:
                merged_result["deployment"] = deploy_result
            status = "succeeded" if succeeded else "failed"
            final_msg = message or ("Build and deployment succeeded" if succeeded else "Deployment to Luma Control failed")
            conn.execute(
                """
                UPDATE builds
                SET status = ?, result_json = ?, message = ?,
                    encrypted_env_secrets = '', completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, json.dumps(merged_result, sort_keys=True), final_msg[:2000], now, now, build_id),
            )
            self._insert_event(
                conn, build_id, "deploy", "status" if succeeded else "error", final_msg, now
            )
            updated = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
            return self._build_public(updated)

    # ------------------------------------------------------------------
    # Queries, cancellation, retry & metrics
    # ------------------------------------------------------------------

    def cancel_build(self, build_id: str) -> dict[str, Any]:
        now = int(time.time())
        with self._conn(write=True) as conn:
            row = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
            if not row:
                raise BuildError(f"build not found: {build_id}", status_code=404)
            st = str(row["status"])
            if st in TERMINAL_STATUSES:
                return self._build_public(row)
            if st in {"queued", "queued_deploy"}:
                conn.execute(
                    "UPDATE builds SET status = 'canceled', cancel_requested = 1, completed_at = ?, updated_at = ?, message = 'Canceled by user' WHERE id = ?",
                    (now, now, build_id),
                )
                self._insert_event(conn, build_id, "queue", "status", "Canceled by user", now)
            else:
                conn.execute(
                    "UPDATE builds SET cancel_requested = 1, updated_at = ?, message = 'Cancellation requested' WHERE id = ?",
                    (now, build_id),
                )
                self._insert_event(conn, build_id, "build", "status", "Cancellation requested", now)
            updated = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
            return self._build_public(updated)

    def get_build(self, build_id: str, *, after_seq: int = 0, limit_events: int = 500) -> dict[str, Any]:
        with self._conn(write=False) as conn:
            row = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
            if not row:
                raise BuildError(f"build not found: {build_id}", status_code=404)
            pub = self._build_public(row)
            events = conn.execute(
                "SELECT seq, stage, kind, message, created_at FROM build_events WHERE build_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (build_id, max(int(after_seq), 0), max(1, min(int(limit_events), 2000))),
            ).fetchall()
            pub["events"] = [
                {
                    "seq": int(e["seq"]),
                    "stage": str(e["stage"]),
                    "kind": str(e["kind"]),
                    "message": str(e["message"]),
                    "createdAt": int(e["created_at"]),
                }
                for e in events
            ]
            return pub

    def list_builds(self, *, limit: int = 30, status: str = "", repo: str = "") -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if repo:
            clauses.append("repo_slug LIKE ?")
            params.append(f"%{repo.strip().lower()}%")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 200)))
        with self._conn(write=False) as conn:
            rows = conn.execute(
                f"SELECT * FROM builds {where} ORDER BY seq DESC LIMIT ?", tuple(params)
            ).fetchall()
            return [self._build_public(r) for r in rows]

    def retry_build(self, build_id: str, *, env_secrets: dict[str, str] | None = None) -> dict[str, Any]:
        with self._conn(write=False) as conn:
            row = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
            if not row:
                raise BuildError(f"build not found: {build_id}", status_code=404)
            req = json.loads(row["request_json"] or "{}")
            if row["pinned_runner"]:
                req["runner"] = str(row["pinned_runner"])
        if env_secrets is not None:
            req["envSecrets"] = env_secrets
        return self.enqueue_build(req)

    def metrics_snapshot(self, *, now: int | None = None) -> dict[str, Any]:
        current = int(time.time() if now is None else now)
        runners = self.list_runners(now=current)
        with self._conn(write=False) as conn:
            counts = {
                str(r["status"]): int(r["cnt"])
                for r in conn.execute("SELECT status, COUNT(*) AS cnt FROM builds GROUP BY status").fetchall()
            }
            oldest_row = conn.execute(
                "SELECT MIN(created_at) AS oldest FROM builds WHERE status = 'queued'"
            ).fetchone()
            oldest_age = (current - int(oldest_row["oldest"])) if (oldest_row and oldest_row["oldest"]) else 0
        return {
            "runners": runners,
            "buildCounts": counts,
            "oldestQueuedSeconds": max(oldest_age, 0),
        }

    def _build_internal(self, row: sqlite3.Row) -> dict[str, Any]:
        pub = self._build_public(row)
        pub["request"] = json.loads(row["request_json"] or "{}")
        return pub

    def _build_public(self, row: sqlite3.Row) -> dict[str, Any]:
        res = json.loads(row["result_json"] or "{}")
        # Strip raw manifest/composeContent blobs from list summaries if huge, keep in result
        req = json.loads(row["request_json"] or "{}")
        req_public = {k: v for k, v in req.items() if k != "manifest"}
        return {
            "id": str(row["id"]),
            "seq": int(row["seq"]),
            "repoUrl": str(row["repo_url"]),
            "repoSlug": str(row["repo_slug"]),
            "ref": str(row["ref"]),
            "targetKey": str(row["target_key"]),
            "status": str(row["status"]),
            "deployEnabled": bool(row["deploy_enabled"]),
            "runner": str(row["runner_name"] or ""),
            "pinnedRunner": str(row["pinned_runner"] or ""),
            "platforms": json.loads(row["platforms_json"] or '["linux/amd64"]'),
            "requiredLabels": json.loads(row["required_labels_json"] or "{}"),
            "request": req_public,
            "result": res,
            "message": str(row["message"] or ""),
            "cancelRequested": bool(row["cancel_requested"]),
            "supersededBy": str(row["superseded_by"] or ""),
            "createdAt": int(row["created_at"] or 0),
            "startedAt": int(row["started_at"] or 0),
            "builtAt": int(row["built_at"] or 0),
            "completedAt": int(row["completed_at"] or 0),
            "updatedAt": int(row["updated_at"] or 0),
        }
