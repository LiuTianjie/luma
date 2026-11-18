"""OCI registry naming, Aliyun ACR 2-level namespace flattening, and HTTP/HTTPS detection."""
from __future__ import annotations

import ipaddress
import re
import urllib.parse

from .errors import BuildError

_TAILSCALE_IPV4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_IPV6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_NAMESPACE_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_SAFE_HOST_RE = re.compile(r"^(?:\[[0-9a-fA-F:]+\]|[a-zA-Z0-9.-]+)(?::[0-9]{1,5})?$")
_GIT_REF_INVALID_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def normalize_registry_host(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise BuildError("registry host is required")
    if "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise BuildError(f"registry host must not include a path or query: {value}")
        raw = parsed.netloc
    raw = raw.strip().rstrip("/")
    if not raw or not _SAFE_HOST_RE.fullmatch(raw):
        raise BuildError(f"invalid registry host: {value}")
    try:
        parsed = urllib.parse.urlsplit("http://" + raw)
        port = parsed.port
    except ValueError as exc:
        raise BuildError(f"invalid registry host: {value}") from exc
    if not parsed.hostname or parsed.username or parsed.password:
        raise BuildError(f"invalid registry host: {value}")
    if port is not None and not (1 <= port <= 65535):
        raise BuildError(f"invalid registry port in host: {value}")
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port:
        return f"{host}:{port}"
    return host


def is_http_registry(host: str, explicit_insecure: bool | None = None) -> bool:
    """Determine whether BuildKit should talk plain HTTP to this registry.

    Public domains (such as Aliyun ACR ``*.aliyuncs.com`` or ``ghcr.io``) always
    use HTTPS unless explicitly overridden. Loopback, RFC1918 private IPs, and
    Tailscale mesh IPs (such as ``100.66.177.70:5000``) default to plain HTTP.
    """
    if explicit_insecure is not None:
        return bool(explicit_insecure)
    normalized = normalize_registry_host(host)
    parsed = urllib.parse.urlsplit("http://" + normalized)
    hostname = (parsed.hostname or "").strip("[]").lower()
    if hostname == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or (ip.version == 4 and ip in _TAILSCALE_IPV4)
        or (ip.version == 6 and ip in _TAILSCALE_IPV6)
    )


def normalize_repo_url(repo_url: str, *, repository: str = "", default_host: str = "github.com") -> str:
    url = str(repo_url or "").strip()
    if not url and repository:
        slug = repository.strip().strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
            raise BuildError(f"invalid repository shorthand: {repository}")
        clean_default_host = normalize_registry_host(default_host)
        url = f"https://{clean_default_host}/{slug}.git"
    if not url:
        raise BuildError("repoUrl or repository is required")
    if url.startswith("-") or any(ord(ch) <= 32 or ord(ch) == 127 for ch in url):
        raise BuildError(f"invalid repository URL: {url}")
    if not url.startswith(("https://", "http://", "ssh://", "git@")):
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", url):
            clean_default_host = normalize_registry_host(default_host)
            url = f"https://{clean_default_host}/{url}.git"
        else:
            url = "https://" + url.lstrip("/")
    if not url.startswith(("https://", "http://", "ssh://", "git@")):
        raise BuildError(f"unsupported repository URL scheme: {url}")
    parsed = urllib.parse.urlsplit(url if "://" in url else "ssh://" + url.replace(":", "/", 1))
    if parsed.query or parsed.fragment:
        raise BuildError("repoUrl must not contain query parameters or fragments")
    if url.startswith(("https://", "http://")) and (parsed.username or parsed.password):
        raise BuildError("repoUrl must not embed credentials; configure Git credentials on luma-build instead")
    return url


def validate_git_ref(value: str) -> str:
    ref = str(value or "main").strip() or "main"
    if ref.startswith("refs/heads/"):
        ref = ref[len("refs/heads/") :]
    if (
        not ref
        or len(ref) > 200
        or ref.startswith("-")
        or ref.endswith(".")
        or ref.endswith(".lock")
        or ".." in ref
        or "@{" in ref
        or "//" in ref
        or _GIT_REF_INVALID_RE.search(ref)
    ):
        raise BuildError(f"invalid Git ref: {value!r}")
    return ref


def derive_repo_slug(repo_url: str) -> str:
    """Extract lowercase ``owner/repo`` from a Git URL."""
    raw = str(repo_url or "").strip()
    if raw.startswith("git@") and ":" in raw:
        path = raw.split(":", 1)[1]
    else:
        parsed = urllib.parse.urlsplit(raw)
        path = parsed.path
    cleaned = path.strip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    parts = [re.sub(r"[^a-z0-9._-]+", "-", p.lower()).strip("-.") for p in cleaned.split("/") if p.strip()]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        raise BuildError(f"cannot derive owner/repo slug from {repo_url}")
    return "/".join(parts[-2:])


def slugify_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    if not token:
        raise BuildError(f"cannot slugify empty identifier: {value!r}")
    return token


def format_image_repository(
    *,
    repo_slug: str,
    service_name: str = "",
    single_service: bool = True,
    namespace: str = "",
    flatten: bool | None = None,
) -> str:
    """Build an OCI repository path compatible with both registry:2 and Aliyun ACR."""
    ns = str(namespace or "").strip().strip("/").lower()
    if ns and not _NAMESPACE_RE.fullmatch(ns):
        raise BuildError(f"invalid registry namespace: {namespace}")
    should_flatten = bool(ns) if flatten is None else bool(flatten)

    segments = [p for p in repo_slug.strip("/").lower().split("/") if p]
    if service_name and not single_service:
        segments.append(slugify_token(service_name))

    if should_flatten:
        leaf = "-".join(slugify_token(seg) for seg in segments)
        leaf = leaf[:120].strip("-")
        return f"{ns}/{leaf}" if ns else leaf

    path = "/".join(segments)
    return f"{ns}/{path}" if ns else path


def format_cache_reference(
    *,
    push_host: str,
    repo_slug: str,
    service_name: str = "",
    platform: str = "linux/amd64",
    namespace: str = "",
    cache_repo: str = "luma-buildcache",
) -> str:
    """Compute a single-repository BuildKit registry cache tag."""
    host = normalize_registry_host(push_host)
    ns = str(namespace or "").strip().strip("/").lower()
    repo_name = slugify_token(cache_repo)
    full_repo = f"{host}/{ns}/{repo_name}" if ns else f"{host}/{repo_name}"

    parts = [slugify_token(seg) for seg in repo_slug.strip("/").split("/") if seg]
    if service_name:
        parts.append(slugify_token(service_name))
    arch_slug = slugify_token(platform.replace("/", "-"))
    tag = f"{'-'.join(parts)[:96]}-{arch_slug}"
    return f"{full_repo}:{tag}"
