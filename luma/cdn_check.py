"""Read-only cache audit for a public domain (``luma cdn check DOMAIN``).

Fetches a few representative URLs anonymously, twice each, and classifies
every response: ``danger`` (could serve one visitor's data to another),
``improve`` (could be cached or compressed) or ``ok``.
"""

from __future__ import annotations

import gzip
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_ASSETS = 6
MAX_URLS = 20
COMPRESS_MIN_BYTES = 1024
CACHE_STATUS_HEADERS = ("x-site-cache-status", "x-cache", "x-cache-status", "cf-cache-status")
ACCOUNT_PATH = re.compile(r"/(account|accounts|user|users|me|login|logout|signin|signup|session|profile|settings|admin|dashboard|cart|checkout|order|orders)(/|$)", re.I)
STATIC_EXTENSIONS = frozenset(
    "css js mjs png jpg jpeg gif webp avif svg ico woff woff2 ttf otf eot mp4 webm mp3 pdf zip gz wasm map".split()
)

Fetch = Callable[[str], Tuple[int, Dict[str, str], bytes]]


def _fetch(url: str) -> Tuple[int, Dict[str, str], bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "luma-cdn-check", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return int(response.status), {k.lower(): v for k, v in response.headers.items()}, response.read(MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        return int(exc.code), {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read(MAX_BODY_BYTES)


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.assets.append(str(values["src"]))
        elif tag == "link" and values.get("href") and "stylesheet" in str(values.get("rel") or "").lower():
            self.assets.append(str(values["href"]))
        elif tag == "img" and values.get("src"):
            self.assets.append(str(values["src"]))


def _same_host_paths(domain: str, html: str) -> List[str]:
    parser = _AssetParser()
    try:
        parser.feed(html)
    except Exception:  # malformed HTML only limits the sample
        pass
    paths: List[str] = []
    for raw in parser.assets:
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin(f"https://{domain}/", raw))
        if parsed.scheme in {"http", "https"} and parsed.hostname == domain and parsed.path:
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            if path not in paths:
                paths.append(path)
    return paths[:MAX_ASSETS]


def _decode(headers: Dict[str, str], body: bytes) -> str:
    if headers.get("content-encoding", "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except OSError:
            return ""
    return body.decode("utf-8", errors="replace")


def _directives(cache_control: str) -> Dict[str, Optional[str]]:
    result: Dict[str, Optional[str]] = {}
    for part in cache_control.split(","):
        name, _, value = part.strip().partition("=")
        if name:
            result[name.lower()] = value.strip('"') or None
    return result


def _seconds(value: Optional[str]) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _extension(path: str) -> str:
    name = urllib.parse.urlsplit(path).path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def classify(path: str, status: int, headers: Dict[str, str], size: int, second: Dict[str, str]) -> Tuple[str, str]:
    """Return ``(verdict, reason)`` for one URL."""
    cache_control = headers.get("cache-control", "")
    directives = _directives(cache_control)
    private = any(name in directives for name in ("private", "no-store", "no-cache"))
    ttl = _seconds(directives.get("s-maxage")) or _seconds(directives.get("max-age"))
    cacheable = not private and ("public" in directives or ttl > 0)
    content_type = headers.get("content-type", "").split(";")[0].strip().lower()
    if cacheable and "set-cookie" in headers:
        return "danger", "cacheable response sets a cookie; drop Set-Cookie or mark it private, no-store"
    if cacheable and ACCOUNT_PATH.search(urllib.parse.urlsplit(path).path):
        return "danger", "account-like path is publicly cacheable; return Cache-Control: private, no-store"
    if status >= 400:
        return "ok", f"HTTP {status}"
    hints: List[str] = []
    extension = _extension(path)
    page_like = not extension or content_type == "text/html"
    if not cache_control and page_like:
        hints.append("no cache headers: if this URL is the same for everyone, add it to cdn.cache or return Cache-Control: public, s-maxage=...")
    elif extension not in STATIC_EXTENSIONS and cache_control and not private and ttl == 0 and status == 200:
        hints.append("revalidated on every request (max-age=0): add s-maxage=60 or list it in cdn.cache if it is the same for everyone")
    elif not cache_control and extension in STATIC_EXTENSIONS:
        hints.append("static file without cache headers: return Cache-Control: public, max-age=... (hashed names: immutable)")
    text_like = content_type.startswith("text/") or content_type in {"application/javascript", "application/json", "application/xml", "image/svg+xml"}
    if text_like and size >= COMPRESS_MIN_BYTES and not headers.get("content-encoding"):
        shown = f">= {MAX_BODY_BYTES // 1048576} MB" if size >= MAX_BODY_BYTES else f"{size} bytes"
        hints.append(f"{shown} of text sent uncompressed (ESA compresses it once enabled)")
    if hints:
        return "improve", "; ".join(hints)
    if cacheable:
        return "ok", "cacheable" + (" (cache hit)" if cache_status(second).startswith("HIT") else "")
    return "ok", "explicitly not cached" if private else "not cached"


def cache_status(headers: Dict[str, str]) -> str:
    for name in CACHE_STATUS_HEADERS:
        if headers.get(name):
            return str(headers[name]).upper()
    if _seconds(headers.get("age")) > 0:
        return f"HIT (age {headers['age']})"
    return ""


def audit_paths(domain: str, *, purge: Optional[Iterable[str]], cache: Iterable[Dict[str, Any]], extra: Iterable[str]) -> List[str]:
    paths = ["/"]
    for path in list(purge or []) + [str(item.get("path") or "") for item in cache] + list(extra):
        if not path.startswith("/"):
            continue
        if path.endswith("*"):
            path = path[:-1] or "/"
        if path not in paths:
            paths.append(path)
    for path in ("/sitemap.xml", "/robots.txt"):
        if path not in paths:
            paths.append(path)
    return paths


def audit_domain(
    domain: str,
    *,
    purge: Optional[Iterable[str]] = None,
    cache: Iterable[Dict[str, Any]] = (),
    extra: Iterable[str] = (),
    fetch: Fetch = _fetch,
) -> List[Dict[str, Any]]:
    paths = audit_paths(domain, purge=purge, cache=cache, extra=extra)
    rows: List[Dict[str, Any]] = []
    index = 0
    while index < len(paths) and len(rows) < MAX_URLS:
        path = paths[index]
        index += 1
        url = f"https://{domain}{path}"
        try:
            status, headers, body = fetch(url)
            _second_status, second, _ = fetch(url)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            rows.append({"path": path, "status": 0, "verdict": "improve", "reason": f"request failed: {exc}"})
            continue
        if path == "/" and status < 400 and "html" in headers.get("content-type", ""):
            for asset in _same_host_paths(domain, _decode(headers, body)):
                if asset not in paths:
                    paths.insert(index, asset)
        size = int(headers.get("content-length") or len(body))
        verdict, reason = classify(path, status, headers, size, second)
        rows.append({
            "path": path,
            "status": status,
            "contentType": headers.get("content-type", "").split(";")[0],
            "cacheControl": headers.get("cache-control", ""),
            "setCookie": "set-cookie" in headers,
            "bytes": size,
            "encoding": headers.get("content-encoding", ""),
            "cache": cache_status(second),
            "verdict": verdict,
            "reason": reason,
        })
    order = {"danger": 0, "improve": 1, "ok": 2}
    return sorted(rows, key=lambda row: order.get(str(row["verdict"]), 3))
