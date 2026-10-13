"""Transactional build metadata. No stdout or workflow credentials in Temporal."""
from __future__ import annotations

import hashlib
import json
import time
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import Settings
from .model import Conflict, NotFound, QueueFull, TERMINAL, decode_cursor, encode_cursor, fingerprint, identifier, public_request


class Store:
    def __init__(self, settings: Settings):
        from cryptography.fernet import Fernet
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        self.settings = settings
        self._local = threading.local()
        self.cipher = Fernet(settings.encryption_key.encode())
        self.pool = ConnectionPool(settings.database_url, min_size=1, max_size=16, kwargs={"row_factory": dict_row}, open=True)

    def close(self):
        self.pool.close()

    def migrate(self):
        with self.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(7180041)")
            exists = conn.execute("SELECT to_regclass('bp_schema') AS name").fetchone()["name"]
            if exists and conn.execute("SELECT coalesce(max(version),0) AS version FROM bp_schema").fetchone()["version"] > 1:
                raise Conflict("build platform database schema is newer than this service")
            conn.execute(Path(__file__).with_name("schema.sql").read_text())

    @contextmanager
    def tx(self):
        current = getattr(self._local, 'connection', None)
        if current is not None:
            with current.transaction():
                yield current
        else:
            with self.pool.connection() as conn, conn.transaction():
                yield conn

    @contextmanager
    def execution_guard(self, run_id):
        # Session advisory lock survives the short transactions below. Using
        # the same connection avoids nested pool acquisition and keeps attempt
        # commits visible to executor leases before Nomad registration returns.
        if getattr(self._local, 'connection', None) is not None:
            yield True
            return
        key = int.from_bytes(hashlib.sha256(('build-execution/' + run_id).encode()).digest()[:8], 'big', signed=True)
        with self.pool.connection() as conn:
            with conn.transaction():
                owned = conn.execute('SELECT pg_try_advisory_lock(%s) AS owned', (key,)).fetchone()['owned']
            if not owned:
                yield False
                return
            self._local.connection = conn
            try:
                yield True
            finally:
                del self._local.connection
                with conn.transaction():
                    conn.execute('SELECT pg_advisory_unlock(%s)', (key,))

    @staticmethod
    def _json(value):
        from psycopg.types.json import Jsonb
        return Jsonb(value)

    def create(self, body: dict[str, Any], *, source: str, project: str, mode="builder", retry_of="", artifact_id="") -> dict:
        now, run_id = int(time.time()), identifier("bld2")
        key = str(body.get("idempotencyKey") or "")
        if len(key) > 256:
            raise Conflict("idempotency key is too long")
        canonical = {k: v for k, v in body.items() if k not in {"queue", "idempotencyKey"}}
        digest = fingerprint(canonical)
        encrypted = self.cipher.encrypt(json.dumps(body).encode())
        with self.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(7180042)")
            if key:
                previous = conn.execute("SELECT * FROM bp_runs WHERE idempotency_key=%s", (key,)).fetchone()
                if previous:
                    if previous["request_hash"] != digest:
                        raise Conflict("idempotency key already belongs to a different request")
                    return previous
            pending = conn.execute("SELECT count(*) AS n FROM bp_runs WHERE status NOT IN ('succeeded','failed','canceled','superseded')").fetchone()["n"]
            if pending >= self.settings.max_active + self.settings.max_pending:
                raise QueueFull("build admission queue is full; retry with the same idempotency key")
            conn.execute(
                "INSERT INTO bp_runs(id,idempotency_key,request_hash,request,private_request,source,project_key,mode,retry_of,artifact_id,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (run_id, key or None, digest, self._json(public_request(body)), encrypted, source, project, mode, retry_of, artifact_id, now, now),
            )
            conn.execute("INSERT INTO bp_outbox(run_id,operation,created_at) VALUES(%s,'start',%s)", (run_id, now))
            self._projection(conn, run_id)
        return self.get(run_id)

    def get(self, run_id: str, *, private=False) -> dict:
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM bp_runs WHERE id=%s", (run_id,)).fetchone()
            if row and row["status"] == "queued":
                row["queuePosition"] = conn.execute("SELECT count(*) AS n FROM bp_runs WHERE status='queued' AND accepted_seq<=%s", (row["accepted_seq"],)).fetchone()["n"]
                row["waitReason"] = "build-capacity"
        if row is None:
            raise NotFound("build not found: " + run_id)
        encrypted = row.pop("private_request")
        if private:
            row["body"] = json.loads(self.cipher.decrypt(bytes(encrypted)))
        return row

    def list(self, query: dict) -> tuple[list[dict], dict]:
        from ..control.history import _time
        limit = int(query.get("limit") or 50)
        if not 1 <= limit <= 100:
            raise Conflict("limit must be between 1 and 100")
        filters = {k: str(query.get(k) or "") for k in ("app", "status", "since", "until", "source")}
        mark = decode_cursor(str(query["cursor"])) if query.get("cursor") else {}
        if mark and mark.get("filter") != fingerprint(filters):
            raise Conflict("cursor does not match build filters")
        with self.tx() as conn:
            watermark = conn.execute("SELECT coalesce(max(accepted_seq),0) AS n FROM bp_runs").fetchone()["n"]
        clauses, args = ["accepted_seq <= %s"], [int(mark.get("watermark", watermark))]
        if mark.get("last"):
            clauses.append("(created_at,id)<(%s,%s)")
            args.extend(mark["last"])
        if filters["status"]:
            clauses.append("status=%s")
            args.append(filters["status"])
        if filters["app"]:
            clauses.append("(project_key=%s OR target_key=%s OR result->>'service'=%s OR result->>'deployment'=%s)")
            args.extend([filters["app"]] * 4)
        for key, operator in (("since", ">="), ("until", "<=")):
            value = _time(filters[key], key)
            if value is not None:
                clauses.append("created_at " + operator + " %s")
                args.append(value)
        if filters["source"] not in {"", "build"}:
            return [], {"limit": limit, "hasMore": False, "nextCursor": None}
        with self.tx() as conn:
            rows = conn.execute("SELECT * FROM bp_runs WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC,id DESC LIMIT %s", (*args, limit + 1)).fetchall()
        more, rows = len(rows) > limit, rows[:limit]
        for row in rows:
            row.pop("private_request", None)
        cursor = encode_cursor({"filter": fingerprint(filters), "watermark": args[0], "last": [rows[-1]["created_at"], rows[-1]["id"]]}) if more else None
        return rows, {"limit": limit, "hasMore": more, "nextCursor": cursor}

    def update(self, run_id: str, **values):
        allowed = {"status", "phase", "build_status", "release_status", "message", "result", "prepared", "artifact_id", "release_id", "target_key", "generation", "capacity_held", "completed_at", "active_platform", "placement_started"}
        if not set(values).issubset(allowed):
            raise ValueError("invalid build update fields")
        values["updated_at"] = int(time.time())
        if values.get("status") in TERMINAL:
            values["completed_at"] = values["updated_at"]
            values["capacity_held"] = False
        with self.tx() as conn:
            row = conn.execute("SELECT status,cancel_requested FROM bp_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
            if row["status"] in TERMINAL:
                return
            if row["cancel_requested"] and values.get("status"):
                values["status"] = "canceled" if values['status'] in TERMINAL else "canceling"
            conn.execute("UPDATE bp_runs SET " + ",".join(key + "=%s" for key in values) + " WHERE id=%s",
                         (*[self._json(v) if k in {"prepared", "result"} else v for k, v in values.items()], run_id))
            self._projection(conn, run_id)


    @staticmethod
    def _projection(conn, run_id):
        conn.execute("INSERT INTO bp_outbox(run_id,operation,created_at) VALUES(%s,'project',%s) ON CONFLICT(run_id,operation) DO UPDATE SET delivered=false,revision=bp_outbox.revision+1", (run_id, int(time.time())))

    def acquire(self, run_id: str, platform: str = "") -> bool:
        with self.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(7180042)")
            row = conn.execute("SELECT * FROM bp_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
            if row["capacity_held"]:
                return True
            if row["cancel_requested"] or row["status"] in TERMINAL:
                return False
            if platform:
                allocating = conn.execute("SELECT count(*) AS n FROM bp_runs WHERE capacity_held AND phase='allocating' AND active_platform=%s", (platform,)).fetchone()["n"]
                if allocating >= 1:
                    return False
            counts = conn.execute("SELECT count(*) AS total,count(*) FILTER(WHERE project_key=%s) AS project FROM bp_runs WHERE capacity_held", (row["project_key"],)).fetchone()
            if counts["total"] >= self.settings.max_active or counts["project"] >= self.settings.max_per_project:
                return False
            ahead = conn.execute("""SELECT count(*) AS total,count(*) FILTER(WHERE q.project_key=%s) AS project FROM bp_runs q WHERE q.status='queued' AND NOT q.cancel_requested
                AND q.accepted_seq<%s AND
                (SELECT count(*) FROM bp_runs a WHERE a.capacity_held AND a.project_key=q.project_key)<%s""",
                (row["project_key"], row["accepted_seq"], self.settings.max_per_project)).fetchone()
            if not platform and (ahead["total"] >= self.settings.max_active - counts["total"] or ahead["project"] >= self.settings.max_per_project - counts["project"]):
                return False
            conn.execute("UPDATE bp_runs SET capacity_held=true,status='running',build_status='preparing',phase=%s,active_platform=%s,placement_started=%s,updated_at=%s WHERE id=%s",
                         ('allocating' if platform else 'preparing', platform, int(time.time()), int(time.time()), run_id))
            self._projection(conn, run_id)
        return True

    def cancel(self, run_id: str) -> dict:
        with self.tx() as conn:
            row = conn.execute("SELECT status FROM bp_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
            if row is None:
                raise NotFound("build not found")
            phase = conn.execute("SELECT phase FROM bp_runs WHERE id=%s", (run_id,)).fetchone()["phase"]
            if phase == "releasing" and row["status"] not in TERMINAL:
                raise Conflict("release has started; wait for its result before rolling back")
            if row["status"] not in TERMINAL:
                conn.execute("UPDATE bp_runs SET cancel_requested=true,status='canceling',updated_at=%s WHERE id=%s", (int(time.time()), run_id))
                conn.execute("INSERT INTO bp_outbox(run_id,operation,created_at) VALUES(%s,'cancel',%s) ON CONFLICT DO NOTHING", (run_id, int(time.time())))
                self._projection(conn, run_id)
        return self.get(run_id)

    def outbox_claim(self) -> dict | None:
        now = int(time.time())
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM bp_outbox WHERE NOT delivered AND claimed_until<%s ORDER BY CASE WHEN operation='project' THEN 1 ELSE 0 END,id FOR UPDATE SKIP LOCKED LIMIT 1", (now,)).fetchone()
            if row:
                conn.execute("UPDATE bp_outbox SET claimed_until=%s,attempts=attempts+1 WHERE id=%s", (now + 30, row["id"]))
            return row

    def outbox_done(self, key: int, revision: int):
        with self.tx() as conn:
            conn.execute("UPDATE bp_outbox SET delivered=(revision=%s),claimed_until=0 WHERE id=%s", (revision, key))

    def attempt(self, run_id: str, number=1, *, token="") -> dict:
        attempt_id = f"{run_id}-a{number}"
        with self.tx() as conn:
            if token:
                now = int(time.time())
                conn.execute("INSERT INTO bp_attempts(id,run_id,number,job_id,token_hash,expires_at,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                             (attempt_id, run_id, number, "luma-" + attempt_id, hashlib.sha256(token.encode()).hexdigest(), now + 86400, now, now))
            row = conn.execute("SELECT * FROM bp_attempts WHERE id=%s", (attempt_id,)).fetchone()
        if not row:
            raise NotFound("build attempt not found")
        return row

    def executor_auth(self, attempt_id: str, token: str) -> dict:
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM bp_attempts WHERE id=%s AND token_hash=%s AND expires_at>%s", (attempt_id, hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
        if not row:
            raise Conflict("invalid or expired executor lease")
        return row

    def receipt(self, attempt_id: str, value: dict):
        with self.tx() as conn:
            row = conn.execute("SELECT result FROM bp_attempts WHERE id=%s FOR UPDATE", (attempt_id,)).fetchone()
            if row is None:
                raise NotFound("attempt not found")
            if row["result"] and row["result"] != value:
                raise Conflict("attempt already has a different immutable receipt")
            conn.execute("UPDATE bp_attempts SET result=%s,updated_at=%s WHERE id=%s", (self._json(value), int(time.time()), attempt_id))

    def save_artifact(self, run_id: str, result: dict, cache_refs: list[str]) -> str:
        run = self.get(run_id)
        artifact_id = "art-" + run_id
        with self.tx() as conn:
            conn.execute("INSERT INTO bp_artifacts(id,run_id,source_commit,recipe_hash,result,cache_refs,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(run_id) DO NOTHING",
                         (artifact_id, run_id, run["prepared"]["sha"], run["prepared"]["recipeHash"], self._json(result), self._json(cache_refs), int(time.time())))
        return artifact_id

    def artifact(self, artifact_id: str) -> dict:
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM bp_artifacts WHERE id=%s", (artifact_id,)).fetchone()
        if row is None:
            raise NotFound("artifact not found")
        return row

    def caches(self, recipe_hash: str) -> list[str]:
        with self.tx() as conn:
            rows = conn.execute("SELECT cache_refs FROM bp_artifacts WHERE recipe_hash=%s ORDER BY created_at DESC LIMIT 3", (recipe_hash,)).fetchall()
        return list(dict.fromkeys(ref for row in rows for ref in row["cache_refs"]))

    def create_release(self, run_id: str, artifact_id: str, target: str) -> dict:
        release_id, now = "rel-" + run_id, int(time.time())
        with self.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(7180043)")
            existing = conn.execute("SELECT * FROM bp_releases WHERE id=%s", (release_id,)).fetchone()
            if existing:
                return existing
            generation = conn.execute("SELECT accepted_seq FROM bp_runs WHERE id=%s", (run_id,)).fetchone()["accepted_seq"]
            conn.execute("INSERT INTO bp_targets(target_key,generation) VALUES(%s,%s) ON CONFLICT(target_key) DO UPDATE SET generation=greatest(bp_targets.generation,excluded.generation)", (target, generation))
            conn.execute("INSERT INTO bp_releases(id,run_id,artifact_id,target_key,generation,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                         (release_id, run_id, artifact_id, target, generation, now, now))
            conn.execute("UPDATE bp_runs SET release_id=%s,artifact_id=%s,target_key=%s,generation=%s WHERE id=%s", (release_id, artifact_id, target, generation, run_id))
            return conn.execute("SELECT * FROM bp_releases WHERE id=%s", (release_id,)).fetchone()

    def release(self, release_id: str) -> dict:
        with self.tx() as conn:
            row = conn.execute("SELECT r.*,t.generation AS desired_generation FROM bp_releases r JOIN bp_targets t ON r.target_key=t.target_key WHERE r.id=%s", (release_id,)).fetchone()
        if row is None:
            raise NotFound("release not found")
        return row

    def finish_release(self, release_id: str, status: str, result: dict, error=""):
        with self.tx() as conn:
            row = conn.execute("UPDATE bp_releases SET status=%s,result=%s,error=%s,updated_at=%s WHERE id=%s RETURNING *", (status, self._json(result), error, int(time.time()), release_id)).fetchone()
            if status == "succeeded":
                conn.execute("UPDATE bp_targets SET applied_generation=%s,active_release=%s WHERE target_key=%s AND generation=%s", (row["generation"], release_id, row["target_key"], row["generation"]))

    def append_chunk(self, run_id: str, batch_id: str, key: str, checksum: str, count: int) -> dict:
        with self.tx() as conn:
            run = conn.execute("SELECT next_event FROM bp_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
            old = conn.execute("SELECT * FROM bp_log_chunks WHERE run_id=%s AND batch_id=%s", (run_id, batch_id)).fetchone()
            if old:
                if old["checksum"] != checksum:
                    raise Conflict("log batch replay does not match stored bytes")
                return old
            first = run["next_event"]
            conn.execute("INSERT INTO bp_log_chunks VALUES(%s,%s,%s,%s,%s,%s,%s)", (run_id, batch_id, first, first + count - 1, key, checksum, int(time.time())))
            conn.execute("UPDATE bp_runs SET next_event=%s WHERE id=%s", (first + count, run_id))
            return {"first_seq": first, "last_seq": first + count - 1}

    def chunks(self, run_id: str, after: int, until: int, limit=100) -> list[dict]:
        with self.tx() as conn:
            return conn.execute("SELECT * FROM bp_log_chunks WHERE run_id=%s AND last_seq>%s AND first_seq<=%s ORDER BY first_seq LIMIT %s", (run_id, after, until, limit)).fetchall()

    def setting(self, key: str, value: dict | None = None) -> dict:
        with self.tx() as conn:
            if value is not None:
                conn.execute("INSERT INTO bp_settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, self._json(value)))
            row = conn.execute("SELECT value FROM bp_settings WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else {}

    def protected_images(self) -> set[str]:
        with self.tx() as conn:
            rows = conn.execute("""SELECT DISTINCT a.result FROM bp_artifacts a JOIN bp_releases r ON r.artifact_id=a.id
                WHERE r.status IN ('queued','running') OR r.id IN (SELECT active_release FROM bp_targets)
                OR r.created_at>%s""", (int(time.time()) - 7 * 86400,)).fetchall()
        return {image for row in rows for image in ([row["result"].get("image", "")] + list((row["result"].get("images") or {}).values())) if image}

    def reserve_target(self, run_id: str, target: str):
        with self.tx() as conn:
            row = conn.execute("SELECT accepted_seq,status,cancel_requested FROM bp_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
            if row['cancel_requested'] or row['status'] in TERMINAL:
                return
            conn.execute("INSERT INTO bp_targets(target_key,generation) VALUES(%s,%s) ON CONFLICT(target_key) DO UPDATE SET generation=greatest(bp_targets.generation,excluded.generation)", (target, row["accepted_seq"]))
            conn.execute("UPDATE bp_runs SET target_key=%s,generation=accepted_seq WHERE id=%s", (target, run_id))

    def attempts(self, run_id: str) -> list[dict]:
        with self.tx() as conn:
            return conn.execute("SELECT * FROM bp_attempts WHERE run_id=%s ORDER BY number", (run_id,)).fetchall()

    def finish_attempt(self, attempt_id: str, status: str):
        with self.tx() as conn:
            conn.execute("UPDATE bp_attempts SET status=%s,expires_at=0,updated_at=%s WHERE id=%s", (status, int(time.time()), attempt_id))

    def begin_release(self, run_id: str) -> bool:
        with self.tx() as conn:
            row = conn.execute("SELECT cancel_requested FROM bp_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
            if row["cancel_requested"]:
                return False
            conn.execute("UPDATE bp_runs SET phase='releasing',release_status='running',capacity_held=false WHERE id=%s", (run_id,))
            return True

    def release_baseline(self, release_id: str, index: int) -> int:
        if index < 0:
            raise Conflict("invalid Nomad modify index")
        with self.tx() as conn:
            row = conn.execute("UPDATE bp_releases SET expected_index=coalesce(expected_index,%s) WHERE id=%s RETURNING expected_index", (index, release_id)).fetchone()
            if row is None:
                raise NotFound("release not found")
            return row["expected_index"]

    def submitted_attempt(self, attempt_id: str):
        with self.tx() as conn:
            conn.execute("UPDATE bp_attempts SET status='submitted',updated_at=%s WHERE id=%s AND status IN ('pending','submitting')", (int(time.time()), attempt_id))

    def freeze_revision(self, run_id: str, sha: str) -> dict:
        with self.tx() as conn:
            row = conn.execute("SELECT prepared FROM bp_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
            if row["prepared"].get("sha"):
                return row["prepared"]
            prepared = {"sha": sha}
            conn.execute("UPDATE bp_runs SET prepared=%s WHERE id=%s", (self._json(prepared), run_id))
            return prepared

    def save_prepared(self, run_id: str, prepared: dict) -> dict:
        with self.tx() as conn:
            row = conn.execute("SELECT prepared FROM bp_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
            if row["prepared"].get("sourceObject"):
                return row["prepared"]
            if row["prepared"].get("sha") != prepared["sha"]:
                raise Conflict("source snapshot does not match the frozen commit")
            conn.execute("UPDATE bp_runs SET prepared=%s WHERE id=%s", (self._json(prepared), run_id))
            return prepared

    def submitting_attempt(self, attempt_id):
        with self.tx() as conn:
            conn.execute("UPDATE bp_attempts SET status='submitting',updated_at=%s WHERE id=%s AND status='pending'", (int(time.time()), attempt_id))
