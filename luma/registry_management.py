from __future__ import annotations

import base64
import concurrent.futures
import datetime as dt
import email.utils
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping

import yaml

from .errors import LumaError
from .registry import normalize_registry_host


MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")


def validate_repository(value: Any) -> str:
    repository = str(value or "").strip().lower().strip("/")
    if not repository or not _REPOSITORY_RE.fullmatch(repository):
        raise LumaError(f"invalid registry repository: {value}")
    return repository


def validate_digest(value: Any) -> str:
    digest = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(digest):
        raise LumaError(f"invalid registry manifest digest: {value}")
    return digest


def managed_image_reference(image: Any, registry_host: str) -> Dict[str, str] | None:
    raw = str(image or "").strip()
    host = normalize_registry_host(registry_host)
    prefix = host + "/"
    if not raw.lower().startswith(prefix.lower()):
        return None
    remainder = raw[len(prefix) :]
    if "@" in remainder:
        repository, digest = remainder.rsplit("@", 1)
        try:
            return {
                "repository": validate_repository(repository),
                "digest": validate_digest(digest),
                "tag": "",
                "reference": raw,
            }
        except LumaError:
            return None
    last = remainder.rsplit("/", 1)[-1]
    if ":" in last:
        repository, tag = remainder.rsplit(":", 1)
    else:
        repository, tag = remainder, "latest"
    try:
        repository = validate_repository(repository)
    except LumaError:
        return None
    tag = str(tag).strip()
    if not tag or len(tag) > 128 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", tag):
        return None
    return {
        "repository": repository,
        "digest": "",
        "tag": tag,
        "reference": raw,
    }


@dataclass(frozen=True)
class RegistryResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class RegistryHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        username: str = "",
        password: str = "",
        timeout: float = 20.0,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise LumaError("registry base URL must use http or https")
        self.username = str(username or "")
        self.password = str(password or "")
        self.timeout = max(float(timeout or 20), 1.0)

    def request(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/json",
        body: bytes | None = None,
        content_type: str = "",
        expected: Iterable[int] = (200,),
    ) -> RegistryResponse:
        headers = {"Accept": accept}
        if self.username or self.password:
            encoded = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method.upper(),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = RegistryResponse(
                    status=int(response.status),
                    headers={str(key): str(value) for key, value in response.headers.items()},
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            result = RegistryResponse(
                status=int(exc.code),
                headers={str(key): str(value) for key, value in exc.headers.items()},
                body=exc.read(),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LumaError(f"registry request failed: {method.upper()} {path}: {exc}") from exc
        allowed = {int(value) for value in expected}
        if result.status not in allowed:
            detail = ""
            try:
                decoded = json.loads(result.body.decode("utf-8"))
                detail = str(decoded.get("errors") or decoded.get("message") or "")[:500]
            except (UnicodeError, json.JSONDecodeError, AttributeError):
                detail = result.body.decode("utf-8", errors="replace")[:500]
            suffix = f": {detail}" if detail else ""
            raise LumaError(f"registry returned HTTP {result.status} for {method.upper()} {path}{suffix}")
        return result

    def catalog(self) -> list[str]:
        repositories: list[str] = []
        path = "/v2/_catalog?n=1000"
        seen: set[str] = set()
        while path:
            response = self.request("GET", path)
            try:
                payload = json.loads(response.body.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise LumaError("registry catalog returned invalid JSON") from exc
            for raw in payload.get("repositories") or []:
                try:
                    repository = validate_repository(raw)
                except LumaError:
                    continue
                if repository not in seen:
                    seen.add(repository)
                    repositories.append(repository)
            path = _next_link(response.headers.get("Link") or response.headers.get("link") or "")
        return sorted(repositories)

    def tags(self, repository: str) -> list[str]:
        repository = validate_repository(repository)
        encoded = urllib.parse.quote(repository, safe="/")
        path = f"/v2/{encoded}/tags/list?n=1000"
        tags: list[str] = []
        seen: set[str] = set()
        while path:
            response = self.request("GET", path, expected=(200, 404))
            if response.status == 404:
                return []
            try:
                payload = json.loads(response.body.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise LumaError(f"registry tag list returned invalid JSON for {repository}") from exc
            for raw in payload.get("tags") or []:
                tag = str(raw or "").strip()
                if tag and tag not in seen:
                    seen.add(tag)
                    tags.append(tag)
            path = _next_link(response.headers.get("Link") or response.headers.get("link") or "")
        return sorted(tags)

    def manifest_head(self, repository: str, reference: str) -> Dict[str, Any]:
        path = _manifest_path(repository, reference)
        response = self.request("HEAD", path, accept=MANIFEST_ACCEPT)
        digest = validate_digest(_header(response.headers, "Docker-Content-Digest"))
        return {
            "digest": digest,
            "mediaType": _header(response.headers, "Content-Type").split(";", 1)[0].strip(),
            "contentLength": int(_header(response.headers, "Content-Length") or 0),
            "lastModified": _http_date_epoch(_header(response.headers, "Last-Modified")),
        }

    def manifest(self, repository: str, reference: str) -> tuple[Dict[str, Any], RegistryResponse]:
        path = _manifest_path(repository, reference)
        response = self.request("GET", path, accept=MANIFEST_ACCEPT)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise LumaError(f"registry manifest returned invalid JSON: {repository}@{reference}") from exc
        if not isinstance(payload, dict):
            raise LumaError(f"registry manifest returned invalid data: {repository}@{reference}")
        return payload, response

    def blob_json(self, repository: str, digest: str) -> Dict[str, Any]:
        repository = validate_repository(repository)
        digest = validate_digest(digest)
        encoded = urllib.parse.quote(repository, safe="/")
        response = self.request("GET", f"/v2/{encoded}/blobs/{digest}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise LumaError(f"registry config blob returned invalid JSON: {repository}@{digest}") from exc
        return payload if isinstance(payload, dict) else {}

    def delete_manifest(self, repository: str, digest: str) -> None:
        # A retried maintenance window may encounter manifests deleted before a
        # previous attempt failed. Treat absence as the desired end state.
        self.request("DELETE", _manifest_path(repository, validate_digest(digest)), expected=(202, 404))

    def put_manifest(self, repository: str, tag: str, manifest: bytes, media_type: str) -> str:
        response = self.request(
            "PUT",
            _manifest_path(repository, tag),
            accept=MANIFEST_ACCEPT,
            body=manifest,
            content_type=media_type,
            expected=(201,),
        )
        return validate_digest(_header(response.headers, "Docker-Content-Digest"))


def scan_registry(client: RegistryHttpClient, *, workers: int = 16) -> Dict[str, Any]:
    started = time.time()
    repositories = client.catalog()
    errors: list[Dict[str, str]] = []

    def load_tags(repository: str) -> tuple[str, list[str], str]:
        try:
            return repository, client.tags(repository), ""
        except LumaError as exc:
            return repository, [], str(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(min(workers, 32), 1)) as pool:
        tag_rows = list(pool.map(load_tags, repositories))

    tag_targets: list[tuple[str, str]] = []
    for repository, tags, error in tag_rows:
        if error:
            errors.append({"repository": repository, "message": error})
        tag_targets.extend((repository, tag) for tag in tags)

    def resolve_tag(target: tuple[str, str]) -> tuple[str, str, Dict[str, Any] | None, str]:
        repository, tag = target
        try:
            return repository, tag, client.manifest_head(repository, tag), ""
        except LumaError as exc:
            return repository, tag, None, str(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(min(workers, 32), 1)) as pool:
        resolved = list(pool.map(resolve_tag, tag_targets))

    grouped: dict[tuple[str, str], Dict[str, Any]] = {}
    for repository, tag, head, error in resolved:
        if error or head is None:
            errors.append({"repository": repository, "tag": tag, "message": error or "manifest unavailable"})
            continue
        key = (repository, str(head["digest"]))
        entry = grouped.setdefault(
            key,
            {
                "repository": repository,
                "digest": str(head["digest"]),
                "tags": [],
                "mediaType": str(head.get("mediaType") or ""),
                "manifestBytes": int(head.get("contentLength") or 0),
                "lastModified": int(head.get("lastModified") or 0),
            },
        )
        entry["tags"].append(tag)
        entry["lastModified"] = max(int(entry.get("lastModified") or 0), int(head.get("lastModified") or 0))

    detail_cache: dict[tuple[str, str], Dict[str, Any]] = {}
    detail_lock = threading.Lock()

    def load_detail(key: tuple[str, str]) -> tuple[tuple[str, str], Dict[str, Any] | None, str]:
        try:
            return key, _manifest_detail(client, key[0], key[1], detail_cache, detail_lock, set()), ""
        except LumaError as exc:
            return key, None, str(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(min(workers // 2, 12), 1)) as pool:
        details = list(pool.map(load_detail, list(grouped)))
    for key, detail, error in details:
        if detail is not None:
            grouped[key].update(detail)
        else:
            errors.append({"repository": key[0], "digest": key[1], "message": error})

    entries = list(grouped.values())
    for entry in entries:
        entry["tags"] = sorted(set(str(tag) for tag in entry.get("tags") or []))
        entry.setdefault("logicalBytes", 0)
        entry.setdefault("createdAt", 0)
        entry.setdefault("platforms", [])
        entry.setdefault("blobDigests", [])
        entry.setdefault("childManifestDigests", [])
    entries.sort(key=lambda item: (str(item["repository"]), -int(item.get("createdAt") or item.get("lastModified") or 0), str(item["digest"])))
    return {
        "repositories": repositories,
        "entries": entries,
        "errors": errors[:200],
        "summary": {
            "repositoryCount": len(repositories),
            "tagCount": len(tag_targets),
            "manifestCount": len(entries),
            "logicalBytes": sum(int(entry.get("logicalBytes") or 0) for entry in entries),
            "scanErrors": len(errors),
            "scannedAt": int(time.time()),
            "durationMs": int((time.time() - started) * 1000),
        },
    }


def apply_protection(
    inventory: Dict[str, Any],
    references: Iterable[Mapping[str, Any]],
    *,
    complete: bool,
    policy: Mapping[str, Any] | None = None,
    now: int | None = None,
) -> Dict[str, Any]:
    current_time = int(now or time.time())
    rules = normalize_policy(policy)
    entries = [dict(item) for item in inventory.get("entries") or [] if isinstance(item, dict)]
    by_digest = {(str(item.get("repository") or ""), str(item.get("digest") or "")): item for item in entries}
    by_child_digest: dict[tuple[str, str], list[Dict[str, Any]]] = {}
    by_tag: dict[tuple[str, str], Dict[str, Any]] = {}
    for item in entries:
        for tag in item.get("tags") or []:
            by_tag[(str(item.get("repository") or ""), str(tag))] = item
        item["protectionReasons"] = []
        for child_digest in item.get("childManifestDigests") or []:
            by_child_digest.setdefault(
                (str(item.get("repository") or ""), str(child_digest)), []
            ).append(item)

    for raw in references:
        repository = str(raw.get("repository") or "")
        digest = str(raw.get("digest") or "")
        tag = str(raw.get("tag") or "")
        if digest:
            direct = by_digest.get((repository, digest))
            targets = [direct] if direct is not None else list(by_child_digest.get((repository, digest), []))
        else:
            tagged = by_tag.get((repository, tag))
            targets = [tagged] if tagged is not None else []
        if not targets:
            continue
        reason = {
            "kind": str(raw.get("kind") or "reference"),
            "source": str(raw.get("source") or raw.get("reference") or ""),
            "reference": str(raw.get("reference") or ""),
        }
        for target in targets:
            if reason not in target["protectionReasons"]:
                target["protectionReasons"].append(reason)

    keep_last = int(rules["keepLast"])
    max_age_seconds = int(rules["maxAgeDays"]) * 86400
    by_repository: dict[str, list[Dict[str, Any]]] = {}
    for item in entries:
        by_repository.setdefault(str(item.get("repository") or ""), []).append(item)
    for repository_entries in by_repository.values():
        repository_entries.sort(key=lambda item: int(item.get("createdAt") or item.get("lastModified") or 0), reverse=True)
        for index, item in enumerate(repository_entries):
            timestamp = int(item.get("createdAt") or item.get("lastModified") or 0)
            recent = timestamp <= 0 or current_time - timestamp < max_age_seconds
            system = _system_repository(str(item.get("repository") or ""))
            if system and index < int(rules["systemKeepLast"]):
                item["protectionReasons"].append({"kind": "system", "source": "system retention", "reference": ""})
            protected = bool(item["protectionReasons"])
            if not complete:
                status = "unknown"
                candidate = False
            elif protected:
                status = "protected"
                candidate = False
            elif rules["mode"] == "off":
                status = "retained"
                candidate = False
            elif index < keep_last or recent:
                status = "retained"
                candidate = False
            else:
                status = "candidate"
                candidate = True
            item["protectionStatus"] = status
            item["deletable"] = candidate
            item["retention"] = {
                "repositoryPosition": index + 1,
                "keptByCount": index < keep_last,
                "keptByAge": recent,
            }
    result = dict(inventory)
    result["entries"] = entries
    result["protectionComplete"] = bool(complete)
    result["policy"] = rules
    summary = dict(result.get("summary") or {})
    summary.update(
        {
            "protectedCount": sum(1 for item in entries if item.get("protectionStatus") == "protected"),
            "retainedCount": sum(1 for item in entries if item.get("protectionStatus") == "retained"),
            "candidateCount": sum(1 for item in entries if item.get("protectionStatus") == "candidate"),
            "unknownCount": sum(1 for item in entries if item.get("protectionStatus") == "unknown"),
        }
    )
    result["summary"] = summary
    return result


def normalize_policy(value: Mapping[str, Any] | None) -> Dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    mode = str(raw.get("mode") or "recommend").strip().lower()
    if mode not in {"off", "recommend", "enforce"}:
        raise LumaError("registry retention mode must be off, recommend, or enforce")
    return {
        "mode": mode,
        "keepLast": _bounded_int(raw.get("keepLast"), default=20, minimum=1, maximum=500),
        "maxAgeDays": _bounded_int(raw.get("maxAgeDays"), default=30, minimum=1, maximum=3650),
        "systemKeepLast": _bounded_int(raw.get("systemKeepLast"), default=3, minimum=1, maximum=100),
        "queueGraceHours": _bounded_int(raw.get("queueGraceHours"), default=24, minimum=1, maximum=24 * 30),
        "gcGraceDays": _bounded_int(raw.get("gcGraceDays"), default=7, minimum=1, maximum=365),
        "warningPercent": _bounded_int(raw.get("warningPercent"), default=75, minimum=1, maximum=99),
        "criticalPercent": _bounded_int(raw.get("criticalPercent"), default=85, minimum=2, maximum=100),
        "emergencyPercent": _bounded_int(raw.get("emergencyPercent"), default=92, minimum=3, maximum=100),
    }


def collect_state_image_references(state: Mapping[str, Any], registry_host: str) -> list[Dict[str, str]]:
    found: list[Dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(image: Any, *, kind: str, source: str) -> None:
        parsed = managed_image_reference(image, registry_host)
        if parsed is None:
            return
        key = (parsed["repository"], parsed["digest"] or parsed["tag"], kind, source)
        if key in seen:
            return
        seen.add(key)
        found.append({**parsed, "kind": kind, "source": source})

    deployments = state.get("deployments") if isinstance(state.get("deployments"), Mapping) else {}
    for bucket_name in ("services", "compose"):
        bucket = deployments.get(bucket_name) if isinstance(deployments.get(bucket_name), Mapping) else {}
        for slug, record in bucket.items():
            if not isinstance(record, Mapping):
                continue
            for image in _images_from_yaml(record.get("manifest")):
                add(image, kind="deployment", source=f"{bucket_name}:{slug}")
            for image in _images_from_yaml(record.get("composeContent")):
                add(image, kind="deployment", source=f"{bucket_name}:{slug}")

    build_runs = state.get("buildRuns") if isinstance(state.get("buildRuns"), Mapping) else {}
    for run_id, run in build_runs.items():
        if not isinstance(run, Mapping):
            continue
        status = str(run.get("status") or "")
        if status not in {"queued", "running", "finalizing", "succeeded"}:
            continue
        for image in _image_values(run.get("result")):
            add(image, kind="build", source=f"build:{run_id}:{status}")

    runtime = state.get("laeRuntime") if isinstance(state.get("laeRuntime"), Mapping) else {}
    runtime_deployments = runtime.get("deployments") if isinstance(runtime.get("deployments"), Mapping) else {}
    for runtime_ref, record in runtime_deployments.items():
        if not isinstance(record, Mapping) or str(record.get("status") or "") in {"deleted"}:
            continue
        for image in _all_strings(record.get("images")):
            add(image, kind="lae", source=f"lae:{runtime_ref}:{record.get('status') or 'unknown'}")

    tasks = state.get("agentTasks") if isinstance(state.get("agentTasks"), Mapping) else {}
    for task_id, task in tasks.items():
        if not isinstance(task, Mapping) or str(task.get("status") or "") not in {"queued", "running"}:
            continue
        for image in _image_values(task.get("payload")):
            add(image, kind="agent-task", source=f"task:{task_id}")
    return found


def _manifest_detail(
    client: RegistryHttpClient,
    repository: str,
    digest: str,
    cache: dict[tuple[str, str], Dict[str, Any]],
    lock: threading.Lock,
    visiting: set[tuple[str, str]],
) -> Dict[str, Any]:
    key = (repository, digest)
    with lock:
        cached = cache.get(key)
    if cached is not None:
        return dict(cached)
    if key in visiting:
        raise LumaError(f"registry manifest cycle detected: {repository}@{digest}")
    visiting = set(visiting)
    visiting.add(key)
    manifest, response = client.manifest(repository, digest)
    media_type = str(manifest.get("mediaType") or _header(response.headers, "Content-Type")).split(";", 1)[0]
    blob_sizes: dict[str, int] = {}
    child_manifest_digests: set[str] = set()
    platforms: set[str] = set()
    created_values: list[int] = []
    if media_type in INDEX_MEDIA_TYPES or isinstance(manifest.get("manifests"), list):
        for descriptor in manifest.get("manifests") or []:
            if not isinstance(descriptor, Mapping):
                continue
            child_digest = str(descriptor.get("digest") or "")
            if not _DIGEST_RE.fullmatch(child_digest):
                continue
            child_manifest_digests.add(child_digest)
            platform = descriptor.get("platform") if isinstance(descriptor.get("platform"), Mapping) else {}
            os_name = str(platform.get("os") or "")
            architecture = str(platform.get("architecture") or "")
            variant = str(platform.get("variant") or "")
            if os_name and architecture:
                platforms.add(f"{os_name}/{architecture}" + (f"/{variant}" if variant else ""))
            child = _manifest_detail(client, repository, child_digest, cache, lock, visiting)
            for blob in child.get("blobs") or []:
                if isinstance(blob, Mapping) and blob.get("digest"):
                    blob_sizes[str(blob["digest"])] = max(int(blob.get("size") or 0), blob_sizes.get(str(blob["digest"]), 0))
            platforms.update(str(value) for value in child.get("platforms") or [] if value)
            if int(child.get("createdAt") or 0):
                created_values.append(int(child["createdAt"]))
    else:
        for descriptor in manifest.get("layers") or []:
            if not isinstance(descriptor, Mapping):
                continue
            blob_digest = str(descriptor.get("digest") or "")
            if _DIGEST_RE.fullmatch(blob_digest):
                blob_sizes[blob_digest] = max(int(descriptor.get("size") or 0), blob_sizes.get(blob_digest, 0))
        config = manifest.get("config") if isinstance(manifest.get("config"), Mapping) else {}
        config_digest = str(config.get("digest") or "")
        if _DIGEST_RE.fullmatch(config_digest):
            blob_sizes[config_digest] = max(int(config.get("size") or 0), blob_sizes.get(config_digest, 0))
            try:
                config_data = client.blob_json(repository, config_digest)
            except LumaError:
                config_data = {}
            created = _iso_epoch(config_data.get("created"))
            if created:
                created_values.append(created)
            os_name = str(config_data.get("os") or "")
            architecture = str(config_data.get("architecture") or "")
            variant = str(config_data.get("variant") or "")
            if os_name and architecture:
                platforms.add(f"{os_name}/{architecture}" + (f"/{variant}" if variant else ""))
    detail = {
        "mediaType": media_type,
        "logicalBytes": sum(blob_sizes.values()),
        "createdAt": min(created_values) if created_values else 0,
        "platforms": sorted(platforms),
        "blobDigests": sorted(blob_sizes),
        "childManifestDigests": sorted(child_manifest_digests),
        "blobs": [{"digest": item, "size": blob_sizes[item]} for item in sorted(blob_sizes)],
    }
    with lock:
        cache[key] = dict(detail)
    return detail


def _images_from_yaml(value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        payload = yaml.safe_load(value)
    except yaml.YAMLError:
        return []
    return _image_values(payload)


def _image_values(value: Any, *, key: str = "") -> list[str]:
    images: list[str] = []
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            name = str(child_key)
            if name.lower() in {"image", "pushimage", "destinationimage", "resolvedimage"} and isinstance(child, str):
                images.append(child)
            elif name.lower() == "images":
                images.extend(_all_strings(child))
            else:
                images.extend(_image_values(child, key=name))
    elif isinstance(value, list):
        for child in value:
            images.extend(_image_values(child, key=key))
    return images


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        result: list[str] = []
        for child in value.values():
            result.extend(_all_strings(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_all_strings(child))
        return result
    return []


def _manifest_path(repository: str, reference: str) -> str:
    repository = validate_repository(repository)
    reference = str(reference or "").strip()
    if not reference or any(character in reference for character in ("/", "?", "#", "\0", "\n", "\r")):
        raise LumaError("invalid registry manifest reference")
    encoded_repository = urllib.parse.quote(repository, safe="/")
    return f"/v2/{encoded_repository}/manifests/{urllib.parse.quote(reference, safe=':')}"


def _next_link(value: str) -> str:
    match = re.search(r"<([^>]+)>\s*;\s*rel=\"?next\"?", str(value or ""), re.IGNORECASE)
    if not match:
        return ""
    parsed = urllib.parse.urlparse(match.group(1))
    return urllib.parse.urlunparse(("", "", parsed.path, parsed.params, parsed.query, ""))


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return ""


def _http_date_epoch(value: str) -> int:
    if not value:
        return 0
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return int(parsed.timestamp())


def _iso_epoch(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp())


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise LumaError("registry policy values must be integers") from exc
    if parsed < minimum or parsed > maximum:
        raise LumaError(f"registry policy value must be between {minimum} and {maximum}")
    return parsed


def _system_repository(repository: str) -> bool:
    return repository in {"luma-control", "luma/control"} or repository.startswith("luma-system/")
