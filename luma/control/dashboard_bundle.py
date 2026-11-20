"""Hot-swappable static asset bundle store for Luma Dashboard.

Stores versioned frontend releases under ``/opt/luma/dashboard/releases/<id>``
(or ``$LUMA_DASHBOARD_DIR`` / ``<state_dir>/dashboard`` in isolated test setups)
and flips ``current -> releases/<id>`` via atomic POSIX symlink replacement.

Control serves ``/dashboard/*`` from:
1. Active hot-swapped bundle (``<store>/current``) when present;
2. Packaged embedded assets (``luma/assets/dashboard``) as local/wheel fallback;
3. Built-in minimal bootstrap HTML when Control runs without embedded assets and
   no bundle has been pulled yet.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import posixpath
import re
import secrets
import shutil
import ssl
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..assets import asset_path
from ..errors import LumaError
from .state import DEFAULT_STATE_DIR, state_dir

DEFAULT_DASHBOARD_BUNDLE_URL = (
    "https://github.com/LiuTianjie/luma/releases/download/dashboard-latest/luma-dashboard.tar.gz"
)
DEFAULT_DASHBOARD_MANIFEST_URL = (
    "https://github.com/LiuTianjie/luma/releases/download/dashboard-latest/manifest.json"
)
RELEASE_META_FILE = ".luma-dashboard-release.json"
MAX_COMPRESSED_BYTES = 32 * 1024 * 1024
MAX_EXTRACTED_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_FILES = 500
MAX_RETAINED_RELEASES = 5
_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_BUNDLE_LOCK = threading.Lock()
_AUTOFETCH_STARTED = False

StepEmitter = Callable[[str, str, str], None]


def dashboard_store_dir() -> Path:
    """Return the root directory for hot-swapped dashboard bundles."""
    override = str(os.environ.get("LUMA_DASHBOARD_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    current_state_dir = state_dir()
    if current_state_dir == DEFAULT_STATE_DIR:
        return Path("/opt/luma/dashboard")
    return current_state_dir / "dashboard"


def _releases_dir(store: Optional[Path] = None) -> Path:
    return (store or dashboard_store_dir()) / "releases"


def _current_link(store: Optional[Path] = None) -> Path:
    return (store or dashboard_store_dir()) / "current"


def embedded_dashboard_root() -> Optional[Path]:
    """Return packaged luma/assets/dashboard path if it contains index.html."""
    try:
        candidate = asset_path("dashboard").resolve()
    except OSError:
        return None
    if (candidate / "index.html").is_file():
        return candidate
    return None


def active_bundle_root(store: Optional[Path] = None) -> Optional[Path]:
    """Return the active hot-swapped release directory if valid."""
    root = store or dashboard_store_dir()
    link = _current_link(root)
    if not link.exists():
        return None
    try:
        resolved = link.resolve()
        releases_root = _releases_dir(root).resolve()
        resolved.relative_to(releases_root)
    except (OSError, ValueError):
        return None
    if resolved.is_dir() and (resolved / "index.html").is_file():
        return resolved
    return None


def resolve_dashboard_root() -> Tuple[Optional[Path], str]:
    """Resolve the active dashboard asset root and mode ('bundle'|'embedded'|'bootstrap')."""
    bundle_root = active_bundle_root()
    if bundle_root is not None:
        return bundle_root, "bundle"
    embedded = embedded_dashboard_root()
    if embedded is not None:
        return embedded, "embedded"
    return None, "bootstrap"


def _read_release_meta(release_dir: Path) -> Dict[str, Any]:
    meta_path = release_dir / RELEASE_META_FILE
    data: Dict[str, Any] = {}
    if meta_path.is_file():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    stat_mtime = 0
    try:
        stat_mtime = int(release_dir.stat().st_mtime)
    except OSError:
        pass
    return {
        "id": str(data.get("id") or release_dir.name),
        "version": str(data.get("version") or release_dir.name),
        "sha256": str(data.get("sha256") or ""),
        "source": str(data.get("source") or "bundle"),
        "sourceUrl": str(data.get("sourceUrl") or ""),
        "installedAt": int(data.get("installedAt") or stat_mtime),
        "fileCount": int(data.get("fileCount") or 0),
        "totalBytes": int(data.get("totalBytes") or 0),
    }


def list_dashboard_releases(store: Optional[Path] = None) -> List[Dict[str, Any]]:
    """List installed dashboard releases newest-first with active indicator."""
    root = store or dashboard_store_dir()
    releases_root = _releases_dir(root)
    if not releases_root.is_dir():
        return []
    active_dir = active_bundle_root(root)
    active_name = active_dir.name if active_dir is not None else ""
    items: List[Dict[str, Any]] = []
    for entry in releases_root.iterdir():
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if not (entry / "index.html").is_file():
            continue
        meta = _read_release_meta(entry)
        meta["active"] = entry.name == active_name
        items.append(meta)
    items.sort(key=lambda item: (item["installedAt"], item["id"]), reverse=True)
    return items


def dashboard_bundle_status(store: Optional[Path] = None) -> Dict[str, Any]:
    """Summarize active dashboard asset mode, release metadata, and history."""
    root = store or dashboard_store_dir()
    active_dir = active_bundle_root(root)
    embedded = embedded_dashboard_root()
    releases = list_dashboard_releases(root)
    if active_dir is not None:
        mode = "bundle"
        active_release = _read_release_meta(active_dir)
        active_release["active"] = True
    elif embedded is not None:
        mode = "embedded"
        active_release = None
    else:
        mode = "bootstrap"
        active_release = None
    return {
        "mode": mode,
        "activeRelease": active_release,
        "embeddedAvailable": embedded is not None,
        "storeDir": str(root),
        "releases": releases,
    }


def _atomic_switch_current(store: Path, release_id: str) -> None:
    link_path = _current_link(store)
    store.mkdir(parents=True, exist_ok=True)
    tmp_link = store / f".current.{os.getpid()}.{secrets.token_hex(4)}"
    target = os.path.join("releases", release_id)
    try:
        os.symlink(target, tmp_link)
        os.replace(tmp_link, link_path)
    finally:
        if tmp_link.is_symlink() or tmp_link.exists():
            try:
                tmp_link.unlink()
            except OSError:
                pass


def _prune_old_releases(store: Path, keep_active: str) -> None:
    releases = list_dashboard_releases(store)
    kept = 0
    for item in releases:
        rid = str(item["id"])
        if rid == keep_active:
            kept += 1
            continue
        if kept < MAX_RETAINED_RELEASES:
            kept += 1
            continue
        target = _releases_dir(store) / rid
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)


def _normalize_member_path(raw_name: str) -> Optional[str]:
    cleaned = str(raw_name or "").replace("\\", "/")
    if "\x00" in cleaned:
        raise LumaError("dashboard bundle contains NUL byte in file path")
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned):
        raise LumaError(f"dashboard bundle contains absolute path: {raw_name!r}")
    normalized = posixpath.normpath(cleaned)
    if normalized in {"", "."}:
        return None
    if normalized == ".." or normalized.startswith("../") or "/../" in normalized:
        raise LumaError(f"dashboard bundle contains unsafe path traversal: {raw_name!r}")
    parts = [part for part in normalized.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise LumaError(f"dashboard bundle contains unsafe path segment: {raw_name!r}")
    return "/".join(parts)


def _sanitize_release_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-.")
    return cleaned[:40]


def install_dashboard_bundle_bytes(
    raw_bytes: bytes,
    *,
    version: str = "",
    source: str = "upload",
    source_url: str = "",
    expected_sha256: str = "",
    store: Optional[Path] = None,
) -> Dict[str, Any]:
    """Validate, atomically extract, and activate a dashboard ``.tar.gz`` bundle."""
    if not raw_bytes:
        raise LumaError("dashboard bundle archive is empty")
    if len(raw_bytes) > MAX_COMPRESSED_BYTES:
        raise LumaError(
            f"dashboard bundle exceeds maximum compressed size ({MAX_COMPRESSED_BYTES // (1024 * 1024)} MiB)"
        )
    digest = hashlib.sha256(raw_bytes).hexdigest()
    clean_expected = str(expected_sha256 or "").strip().lower()
    if clean_expected:
        if not _SHA256_RE.fullmatch(clean_expected):
            raise LumaError(f"invalid expected sha256: {expected_sha256!r}")
        if digest != clean_expected:
            raise LumaError(f"dashboard bundle sha256 mismatch: expected {clean_expected}, got {digest}")

    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:*")
    except (tarfile.TarError, OSError) as exc:
        raise LumaError(f"invalid dashboard bundle archive: {exc}") from exc

    extracted_files: List[Tuple[str, bytes]] = []
    total_bytes = 0
    with archive:
        for member in archive.getmembers():
            if member.issym() or member.islnk():
                raise LumaError(f"dashboard bundle must not contain symlinks or hardlinks: {member.name!r}")
            if member.isdir():
                _normalize_member_path(member.name)
                continue
            if not member.isreg():
                raise LumaError(f"dashboard bundle contains unsupported entry type: {member.name!r}")
            rel = _normalize_member_path(member.name)
            if not rel:
                continue
            # Skip archive metadata dotfiles (e.g. macOS AppleDouble / .DS_Store)
            basename = posixpath.basename(rel)
            if basename in {".DS_Store", RELEASE_META_FILE} or basename.startswith("._"):
                continue
            if len(extracted_files) >= MAX_BUNDLE_FILES:
                raise LumaError(f"dashboard bundle exceeds maximum file count ({MAX_BUNDLE_FILES})")
            if member.size < 0 or total_bytes + member.size > MAX_EXTRACTED_BYTES:
                raise LumaError(
                    f"dashboard bundle exceeds maximum extracted size ({MAX_EXTRACTED_BYTES // (1024 * 1024)} MiB)"
                )
            handle = archive.extractfile(member)
            if handle is None:
                continue
            data = handle.read(MAX_EXTRACTED_BYTES - total_bytes + 1)
            total_bytes += len(data)
            if total_bytes > MAX_EXTRACTED_BYTES:
                raise LumaError(
                    f"dashboard bundle exceeds maximum extracted size ({MAX_EXTRACTED_BYTES // (1024 * 1024)} MiB)"
                )
            extracted_files.append((rel, data))

    if not extracted_files:
        raise LumaError("dashboard bundle contains no files")

    # Strip a single common top-level wrapper directory (e.g. `dashboard/index.html`)
    if not any(rel == "index.html" for rel, _ in extracted_files):
        top_dirs = {rel.split("/", 1)[0] for rel, _ in extracted_files if "/" in rel}
        all_nested = all("/" in rel for rel, _ in extracted_files)
        if all_nested and len(top_dirs) == 1:
            prefix = next(iter(top_dirs)) + "/"
            extracted_files = [(rel.removeprefix(prefix), data) for rel, data in extracted_files]

    index_entries = [data for rel, data in extracted_files if rel == "index.html"]
    if not index_entries or not index_entries[0].strip():
        raise LumaError("dashboard bundle is missing index.html")

    root = store or dashboard_store_dir()
    releases_root = _releases_dir(root)
    now = int(time.time())
    ver_label = str(version or "").strip() or f"bundle-{digest[:12]}"
    ver_seg = _sanitize_release_segment(ver_label)
    release_id = f"{now}-{ver_seg}-{digest[:8]}" if ver_seg else f"{now}-{digest[:12]}"
    if not _RELEASE_ID_RE.fullmatch(release_id):
        release_id = f"{now}-{digest[:12]}"

    meta = {
        "id": release_id,
        "version": ver_label,
        "sha256": digest,
        "source": str(source or "upload").strip() or "upload",
        "sourceUrl": str(source_url or "").strip(),
        "installedAt": now,
        "fileCount": len(extracted_files),
        "totalBytes": total_bytes,
    }

    with _BUNDLE_LOCK:
        releases_root.mkdir(parents=True, exist_ok=True)
        staging = releases_root / f".staging-{release_id}-{secrets.token_hex(4)}"
        final_dir = releases_root / release_id
        if final_dir.exists():
            release_id = f"{release_id}-{secrets.token_hex(3)}"
            meta["id"] = release_id
            final_dir = releases_root / release_id
        try:
            staging.mkdir(parents=True, exist_ok=False)
            staging_resolved = staging.resolve()
            for rel, data in extracted_files:
                dest = (staging / rel).resolve()
                dest.relative_to(staging_resolved)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            (staging / RELEASE_META_FILE).write_text(
                json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(staging, final_dir)
            _atomic_switch_current(root, release_id)
            _prune_old_releases(root, release_id)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    meta["active"] = True
    return {
        "release": meta,
        "status": dashboard_bundle_status(root),
    }


def rollback_dashboard_bundle(
    release_id: str = "",
    *,
    store: Optional[Path] = None,
) -> Dict[str, Any]:
    """Atomically roll back the active dashboard symlink to an earlier release."""
    root = store or dashboard_store_dir()
    with _BUNDLE_LOCK:
        releases = list_dashboard_releases(root)
        if not releases:
            raise LumaError("no hot-swapped dashboard releases are installed")
        wanted = str(release_id or "").strip()
        if wanted:
            if not _RELEASE_ID_RE.fullmatch(wanted):
                raise LumaError(f"invalid dashboard release id: {wanted!r}")
            target = next((item for item in releases if item["id"] == wanted), None)
            if target is None:
                raise LumaError(f"dashboard release not found: {wanted}")
        else:
            previous = [item for item in releases if not item.get("active")]
            if not previous:
                raise LumaError("no previous dashboard release is available to roll back to")
            target = previous[0]
        target_id = str(target["id"])
        target_dir = _releases_dir(root) / target_id
        if not (target_dir / "index.html").is_file():
            raise LumaError(f"dashboard release {target_id} is missing index.html")
        _atomic_switch_current(root, target_id)
        result_meta = _read_release_meta(target_dir)
        result_meta["active"] = True
    return {
        "release": result_meta,
        "status": dashboard_bundle_status(root),
    }


def _validate_remote_url(url: str) -> str:
    clean = str(url or "").strip()
    if not clean:
        raise LumaError("dashboard bundle URL is required")
    parsed = urllib.parse.urlsplit(clean)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme == "https" and host:
        return clean
    if scheme == "http" and host in _LOOPBACK_HOSTS:
        return clean
    raise LumaError(f"dashboard bundle URL must use https:// (got {clean!r})")


def _resolve_cluster_egress_proxy() -> str:
    env_proxy = str(
        os.environ.get("LUMA_DASHBOARD_PROXY_URL")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or ""
    ).strip()
    if env_proxy:
        return env_proxy
    try:
        from . import server, state as control_state

        current = control_state.load_auth_state()
        return str(server._egress_gateway_proxy_url(current) or "").strip()
    except Exception:
        return ""


def _http_get_bytes(url: str, *, max_bytes: int, timeout: float = 30.0) -> bytes:
    clean_url = _validate_remote_url(url)
    host = (urllib.parse.urlsplit(clean_url).hostname or "").lower()
    insecure = str(os.environ.get("LUMA_DASHBOARD_INSECURE") or "").strip().lower() in {"1", "true", "yes"}
    ctx = ssl._create_unverified_context() if insecure else None

    def _fetch(proxy_url: str = "") -> bytes:
        req = urllib.request.Request(
            clean_url,
            headers={"User-Agent": "luma-control/dashboard-bundle"},
            method="GET",
        )
        handlers: List[Any] = []
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        if ctx is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(req, timeout=timeout) as resp:
            chunks: List[bytes] = []
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise LumaError(f"remote dashboard download exceeded limit ({max_bytes // (1024 * 1024)} MiB)")
                chunks.append(chunk)
            return b"".join(chunks)

    proxy_url = "" if host in _LOOPBACK_HOSTS else _resolve_cluster_egress_proxy()
    attempts = [proxy_url, ""] if proxy_url else [""]
    last_exc: Optional[Exception] = None
    for candidate_proxy in attempts:
        try:
            return _fetch(candidate_proxy)
        except urllib.error.HTTPError as exc:
            # HTTP 4xx/5xx from origin (e.g. 404) shouldn't be retried through a different path unless proxy error
            if not candidate_proxy or exc.code not in {407, 502, 503, 504}:
                raise LumaError(f"failed to fetch {clean_url}: HTTP {exc.code}") from exc
            last_exc = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
    raise LumaError(f"failed to fetch {clean_url}: {last_exc}") from last_exc


def fetch_remote_manifest(manifest_url: str, *, timeout: float = 10.0) -> Dict[str, Any]:
    raw = _http_get_bytes(manifest_url, max_bytes=256 * 1024, timeout=timeout)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LumaError(f"invalid dashboard manifest JSON from {manifest_url}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LumaError("dashboard manifest must be a JSON object")
    return parsed


def check_remote_dashboard_update(
    manifest_url: str,
    *,
    bundle_url: str = "",
    store: Optional[Path] = None,
) -> Dict[str, Any]:
    """Compare remote manifest metadata against the active dashboard bundle."""
    status = dashboard_bundle_status(store)
    clean_manifest_url = str(manifest_url or "").strip()
    if not clean_manifest_url:
        return {
            "checked": False,
            "updateAvailable": False,
            "reason": "no_manifest_url",
            "current": status["activeRelease"],
            "mode": status["mode"],
            "latest": None,
        }
    manifest = fetch_remote_manifest(clean_manifest_url)
    latest_sha = str(manifest.get("sha256") or "").strip().lower()
    latest_version = str(manifest.get("version") or "").strip()
    current = status["activeRelease"]
    current_sha = str((current or {}).get("sha256") or "").strip().lower()
    if status["mode"] != "bundle":
        update_available = True
    elif latest_sha:
        update_available = latest_sha != current_sha
    else:
        update_available = bool(latest_version and latest_version != str((current or {}).get("version") or ""))
    return {
        "checked": True,
        "updateAvailable": update_available,
        "mode": status["mode"],
        "current": current,
        "latest": {
            "version": latest_version,
            "sha256": latest_sha,
            "commit": str(manifest.get("commit") or ""),
            "builtAt": int(manifest.get("builtAt") or 0),
            "bundleUrl": str(manifest.get("bundleUrl") or bundle_url or ""),
        },
    }


def install_dashboard_bundle_from_url(
    bundle_url: str,
    *,
    manifest_url: str = "",
    version: str = "",
    expected_sha256: str = "",
    emit: Optional[StepEmitter] = None,
    store: Optional[Path] = None,
) -> Dict[str, Any]:
    """Download a remote dashboard bundle tarball and atomically hot-swap it."""
    clean_bundle_url = _validate_remote_url(bundle_url)
    resolved_version = str(version or "").strip()
    resolved_sha = str(expected_sha256 or "").strip().lower()
    clean_manifest_url = str(manifest_url or "").strip()

    if clean_manifest_url and (not resolved_sha or not resolved_version):
        if emit:
            emit("running", "Inspect release manifest", clean_manifest_url)
        try:
            manifest = fetch_remote_manifest(clean_manifest_url)
            if not resolved_version and manifest.get("version"):
                resolved_version = str(manifest["version"]).strip()
            if not resolved_sha and manifest.get("sha256"):
                resolved_sha = str(manifest["sha256"]).strip().lower()
            if emit:
                emit(
                    "ok",
                    "Inspect release manifest",
                    f"version={resolved_version or 'latest'} sha256={(resolved_sha[:12] if resolved_sha else 'unverified')}",
                )
        except LumaError as exc:
            if emit:
                emit("ok", "Inspect release manifest", f"skipped ({exc})")

    if emit:
        emit("running", "Download dashboard bundle", clean_bundle_url)
    archive_bytes = _http_get_bytes(clean_bundle_url, max_bytes=MAX_COMPRESSED_BYTES, timeout=60.0)
    if emit:
        emit(
            "ok",
            "Download dashboard bundle",
            f"downloaded {len(archive_bytes) / 1024:.1f} KiB",
        )

    if emit:
        emit("running", "Verify and activate bundle", str(store or dashboard_store_dir()))
    installed = install_dashboard_bundle_bytes(
        archive_bytes,
        version=resolved_version,
        source="remote",
        source_url=clean_bundle_url,
        expected_sha256=resolved_sha,
        store=store,
    )
    rel = installed["release"]
    if emit:
        emit(
            "ok",
            "Verify and activate bundle",
            f"active release {rel['version']} ({rel['sha256'][:12]}, {rel['fileCount']} files)",
        )
    return installed


def package_dashboard_directory(
    source_dir: Path,
    *,
    version: str = "",
    commit: str = "",
    bundle_url: str = "",
) -> Tuple[bytes, Dict[str, Any]]:
    """Package a built Vite dashboard directory into ``luma-dashboard.tar.gz`` + manifest."""
    root = Path(source_dir).expanduser().resolve()
    if not (root / "index.html").is_file():
        raise LumaError(f"built dashboard directory is missing index.html: {root}")
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.name != RELEASE_META_FILE
    )
    if not files:
        raise LumaError(f"built dashboard directory is empty: {root}")
    buf = io.BytesIO()
    total_bytes = 0
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tf:
        for file_path in files:
            rel = file_path.relative_to(root).as_posix()
            raw = file_path.read_bytes()
            total_bytes += len(raw)
            info = tarfile.TarInfo(name=rel)
            info.size = len(raw)
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            tf.addfile(info, io.BytesIO(raw))
    archive_bytes = buf.getvalue()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    now = int(time.time())
    manifest = {
        "schemaVersion": "luma.dashboard-bundle/v1",
        "version": str(version or "").strip() or f"bundle-{digest[:12]}",
        "commit": str(commit or "").strip(),
        "sha256": digest,
        "compressedBytes": len(archive_bytes),
        "totalBytes": total_bytes,
        "fileCount": len(files),
        "builtAt": now,
    }
    if bundle_url:
        manifest["bundleUrl"] = str(bundle_url).strip()
    return archive_bytes, manifest


def bootstrap_html() -> bytes:
    """Minimal self-contained fallback page when no dashboard bundle is installed yet."""
    html = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Luma · 控制台引导 / Dashboard Bootstrap</title>
<style>
  :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; background: #f8fafc; color: #0f172a; }
  @media (prefers-color-scheme: dark) { body { background: #09090b; color: #f4f4f5; } .card { background: #18181b; border-color: #27272a; } code, pre, input { background: #09090b; border-color: #3f3f46; color: #f4f4f5; } }
  .card { width: 100%; max-width: 560px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 14px; padding: 28px; box-shadow: 0 10px 30px rgba(15,23,42,.08); }
  h1 { margin: 0 0 8px; font-size: 20px; }
  p { margin: 0 0 14px; font-size: 14px; line-height: 1.6; color: #64748b; }
  pre { margin: 12px 0 18px; padding: 12px 14px; border-radius: 8px; border: 1px solid #e2e8f0; background: #f8fafc; font-size: 12px; overflow-x: auto; }
  label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
  input { width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px; border: 1px solid #cbd5e1; font-size: 13px; margin-bottom: 12px; }
  button { padding: 10px 16px; border-radius: 8px; border: 0; background: #2563eb; color: #fff; font-size: 13px; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: 0.6; cursor: not-allowed; }
  #msg { margin-top: 12px; font-size: 13px; white-space: pre-wrap; }
</style>
</head>
<body>
  <div class="card">
    <h1>Luma Dashboard 静态包尚未安装</h1>
    <p>Luma Control 已就绪，控制台前端以独立静态包托管在 <code>/opt/luma/dashboard</code>，无需重打控制面镜像即可热更新。</p>
    <p>可通过 CLI 安装或从本地构建直传：</p>
    <pre>luma addon install dashboard
# 或本地构建直传（无需打镜像）：
luma addon install dashboard --source ./dashboard-src</pre>
    <form id="f">
      <label for="tok">或输入管理 Token 在此一键拉取官方最新控制台包</label>
      <input id="tok" type="password" placeholder="LUMA_DEPLOY_TOKEN" autocomplete="off" required />
      <button id="btn" type="submit">一键拉取并激活 Dashboard</button>
    </form>
    <div id="msg"></div>
  </div>
  <script>
    const f = document.getElementById('f'), btn = document.getElementById('btn'), msg = document.getElementById('msg');
    f.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const token = document.getElementById('tok').value.trim();
      if (!token) return;
      btn.disabled = true;
      msg.textContent = '正在从发布源拉取并校验 Luma Dashboard 静态包…';
      try {
        const res = await fetch('/v1/addons/dashboard/apply', {
          method: 'POST',
          headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
          body: '{}'
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
        msg.textContent = '已激活最新版本，正在刷新…';
        window.location.reload();
      } catch (err) {
        msg.textContent = '拉取失败：' + (err && err.message ? err.message : String(err));
        btn.disabled = false;
      }
    });
  </script>
</body>
</html>
"""
    return html.encode("utf-8")


def maybe_autofetch_dashboard_bundle_async(
    *,
    bundle_url: str = "",
    manifest_url: str = "",
) -> bool:
    """Kick off a non-blocking background fetch when running in bootstrap mode."""
    global _AUTOFETCH_STARTED
    flag = str(os.environ.get("LUMA_DASHBOARD_AUTO_FETCH", "1")).strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    _root, mode = resolve_dashboard_root()
    if mode != "bootstrap":
        return False
    with _BUNDLE_LOCK:
        if _AUTOFETCH_STARTED:
            return False
        _AUTOFETCH_STARTED = True

    target_bundle = str(bundle_url or DEFAULT_DASHBOARD_BUNDLE_URL).strip()
    target_manifest = str(manifest_url or DEFAULT_DASHBOARD_MANIFEST_URL).strip()

    def _worker() -> None:
        try:
            install_dashboard_bundle_from_url(target_bundle, manifest_url=target_manifest)
        except Exception:
            # Non-fatal: operators can always pull or upload via CLI or the bootstrap page.
            pass

    thread = threading.Thread(target=_worker, name="luma-dashboard-autofetch", daemon=True)
    thread.start()
    return True
