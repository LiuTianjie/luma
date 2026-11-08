"""Alibaba Cloud ESA (Edge Security Acceleration) integration for Luma services.

A Luma zone is one ESA site joined by CNAME. For each service with ``cdn: esa``
Luma creates a proxied ESA record pointing at the edge, points the Cloudflare
DNS record at the ESA CNAME, lets ESA issue its free certificate and refreshes
the cache after each rollout. Site-wide configs make ESA honour the
application's ``Cache-Control`` headers on every path (ESA alone ignores them
for HTML); per-domain rules cache the manifest's ``cdn.cache`` paths.

Requests are signed with ACS3-HMAC-SHA256 using the standard library, so the
package keeps no Alibaba Cloud SDK dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets as secrets_module
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import LumaError

CDN_PROVIDERS = frozenset({"esa"})
CDN_SCOPES = frozenset({"global", "domestic", "overseas"})
CDN_EXPOSURES = frozenset({"cn-edge", "external-edge", "tailscale-relay"})
CDN_FIELDS = frozenset({"provider", "scope", "purge", "cache"})
CACHE_FIELDS = frozenset({"path", "ttl"})
# Cache rules count against a per-site quota shared by every app on the root
# domain (Free 5, Basic 10, Standard 50); keep one app from taking it all.
MAX_CACHE_PATHS = 8
MAX_CACHE_TTL = 31536000

# Prefixed so they never collide with an application's own Alibaba Cloud keys.
ESA_KEY_ID_ENV = "LUMA_ALIBABA_CLOUD_ACCESS_KEY_ID"
ESA_KEY_SECRET_ENV = "LUMA_ALIBABA_CLOUD_ACCESS_KEY_SECRET"
ESA_SECRET_NAMES = (ESA_KEY_ID_ENV, ESA_KEY_SECRET_ENV)

ESA_ENDPOINT = "esa.cn-hangzhou.aliyuncs.com"
ESA_VERSION = "2024-09-10"

API_ATTEMPTS = 4
API_TIMEOUT_SECONDS = 20
VERIFY_TIMEOUT_SECONDS = 180
VERIFY_POLL_SECONDS = 10.0
CERTIFICATE_TIMEOUT_SECONDS = 900
CERTIFICATE_POLL_SECONDS = 15.0
# Rejections that guarantee nothing was applied: safe to retry any action.
THROTTLE_CODE_PREFIXES = ("Throttling", "TooManyRequests", "Site.ServiceBusy", "Record.ServiceBusy", "Record.Reserved")
# Network errors and 5xx may hide an applied write, so only reads retry them;
# writes are reconciled by the next deploy instead.
READ_ACTION_PREFIXES = ("List", "Get", "Describe", "Verify")


# ---------------------------------------------------------------------------
# Manifest block
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CachePath:
    path: str
    ttl: int


@dataclass(frozen=True)
class CdnSpec:
    provider: str = "esa"
    scope: str = "global"
    # None refreshes the whole hostname after each deploy; () disables refresh.
    purge: Optional[Tuple[str, ...]] = None
    # Paths that are the same for every visitor: cached for ``ttl`` when the
    # application sends no cache headers of its own.
    cache: Tuple[CachePath, ...] = ()


def parse_cdn_block(raw: Any, *, label: str = "cdn") -> Optional[CdnSpec]:
    """Parse ``cdn: esa`` or the mapping form; ``None``/``false`` disable it."""
    if raw is None or raw is False:
        return None
    if isinstance(raw, str):
        raw = {"provider": raw}
    if not isinstance(raw, Mapping):
        raise LumaError(f"{label} must be a provider name or a mapping")
    unknown = sorted(str(key) for key in raw if str(key) not in CDN_FIELDS)
    if unknown:
        raise LumaError(f"unsupported {label} field(s): {', '.join(unknown)}")
    provider = str(raw.get("provider") or "").strip()
    if provider not in CDN_PROVIDERS:
        raise LumaError(f"{label}.provider must be one of {sorted(CDN_PROVIDERS)}")
    scope = str(raw.get("scope") or "global").strip()
    if scope not in CDN_SCOPES:
        raise LumaError(f"{label}.scope must be one of {sorted(CDN_SCOPES)}")
    cache = _parse_cache_paths(raw.get("cache"), label=f"{label}.cache")
    if raw.get("purge") is None:
        return CdnSpec(provider=provider, scope=scope, purge=None, cache=cache)
    purge_raw = raw.get("purge")
    if not isinstance(purge_raw, list) or any(not isinstance(item, str) for item in purge_raw):
        raise LumaError(f"{label}.purge must be a list of URL paths")
    purge: List[str] = []
    for item in purge_raw:
        path = item.strip()
        if not path.startswith("/") or "://" in path or any(ch.isspace() for ch in path):
            raise LumaError(f"{label}.purge entries must be URL paths starting with /: {item!r}")
        if "*" in path and not (path.endswith("/*") and path.count("*") == 1):
            raise LumaError(f"{label}.purge only supports * as a trailing /* directory: {item!r}")
        if path not in purge:
            purge.append(path)
    return CdnSpec(provider=provider, scope=scope, purge=tuple(purge), cache=cache)


def _parse_cache_paths(raw: Any, *, label: str) -> Tuple[CachePath, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise LumaError(f"{label} must be a list of {{path, ttl}} entries")
    if len(raw) > MAX_CACHE_PATHS:
        raise LumaError(f"{label} supports at most {MAX_CACHE_PATHS} paths")
    result: List[CachePath] = []
    for index, item in enumerate(raw):
        where = f"{label}[{index}]"
        if not isinstance(item, Mapping):
            raise LumaError(f"{where} must be a mapping with path and ttl")
        unknown = sorted(str(key) for key in item if str(key) not in CACHE_FIELDS)
        if unknown:
            raise LumaError(f"unsupported {where} field(s): {', '.join(unknown)}")
        path = str(item.get("path") or "").strip()
        if not path.startswith("/") or any(ch.isspace() or ch == '"' for ch in path) or "*" in path[:-1]:
            raise LumaError(f"{where}.path must start with / and may only end with *: {path!r}")
        ttl = item.get("ttl")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= MAX_CACHE_TTL:
            raise LumaError(f"{where}.ttl must be seconds between 1 and {MAX_CACHE_TTL}")
        if any(existing.path == path for existing in result):
            raise LumaError(f"{where}.path is listed twice: {path}")
        result.append(CachePath(path=path, ttl=ttl))
    for first in result:
        for second in result:
            if first is not second and first.path.endswith("*") and second.path.startswith(first.path[:-1]):
                raise LumaError(f"{label} paths overlap: {first.path} covers {second.path}")
    return tuple(result)


def validate_cdn_exposure(cdn: Optional[CdnSpec], exposure: str, *, label: str = "cdn") -> None:
    if cdn is not None and exposure not in CDN_EXPOSURES:
        raise LumaError(f"{label} requires exposure {', '.join(sorted(CDN_EXPOSURES))}; got {exposure}")


def purge_requests(domain: str, paths: Optional[Iterable[str]]) -> List[Dict[str, Any]]:
    """Translate manifest purge paths into ESA PurgeCaches requests."""
    if paths is None:
        return [{"Type": "hostname", "Content": {"Hostnames": [domain]}, "Force": False}]
    files: List[str] = []
    directories: List[str] = []
    for path in paths:
        if path.endswith("/*"):
            directories.append(f"https://{domain}{path[:-1]}")
        else:
            files.append(f"https://{domain}{path}")
    requests: List[Dict[str, Any]] = []
    if files:
        requests.append({"Type": "file", "Content": {"Files": files}})
    if directories:
        requests.append({"Type": "directory", "Content": {"Directories": directories}, "Force": False})
    return requests


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def esa_credentials(state: Mapping[str, Any]) -> Tuple[str, str]:
    stored = state.get("secrets") if isinstance(state.get("secrets"), Mapping) else {}
    key_id = str(stored.get(ESA_KEY_ID_ENV) or os.environ.get(ESA_KEY_ID_ENV) or "").strip()
    key_secret = str(stored.get(ESA_KEY_SECRET_ENV) or os.environ.get(ESA_KEY_SECRET_ENV) or "").strip()
    missing = [name for name, value in ((ESA_KEY_ID_ENV, key_id), (ESA_KEY_SECRET_ENV, key_secret)) if not value]
    if missing:
        raise LumaError(f"missing Alibaba Cloud ESA credential(s): {', '.join(missing)}; set them with luma secret set")
    return key_id, key_secret


# ---------------------------------------------------------------------------
# ACS3-HMAC-SHA256 client
# ---------------------------------------------------------------------------


def _percent(value: str) -> str:
    return urllib.parse.quote(str(value), safe="-_.~")


def _encode_params(params: Mapping[str, Any]) -> str:
    return "&".join(f"{_percent(key)}={_percent(params[key])}" for key in sorted(params))


def acs3_signature(
    *,
    method: str,
    host: str,
    headers: Mapping[str, str],
    query: str,
    body: bytes,
    key_id: str,
    key_secret: str,
) -> Tuple[str, str]:
    """Return ``(Authorization header, signature)`` for an ACS3 request.

    ``headers`` must already contain every ``x-acs-*`` header and, when there
    is a body, ``content-type``.  ``host`` is always signed.
    """
    signed = {str(name).lower(): str(value).strip() for name, value in headers.items()}
    signed["host"] = host
    names = sorted(name for name in signed if name.startswith("x-acs-") or name in {"host", "content-type"})
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
    signed_headers = ";".join(names)
    hashed_payload = hashlib.sha256(body).hexdigest()
    canonical_request = "\n".join(
        [method.upper(), "/", query, canonical_headers, signed_headers, hashed_payload]
    )
    string_to_sign = "ACS3-HMAC-SHA256\n" + hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    signature = hmac.new(key_secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = f"ACS3-HMAC-SHA256 Credential={key_id},SignedHeaders={signed_headers},Signature={signature}"
    return authorization, signature


class AliyunApiError(LumaError):
    def __init__(self, action: str, status: int, code: str, message: str, request_id: str = ""):
        self.action = action
        self.status = status
        self.code = code
        self.api_message = message
        self.request_id = request_id
        suffix = f" (RequestId {request_id})" if request_id else ""
        super().__init__(f"Alibaba Cloud {action} failed: {code}: {message}{suffix}")


class AliyunClient:
    def __init__(self, key_id: str, key_secret: str, *, sleep: Callable[[float], None] = time.sleep):
        self.key_id = key_id
        self.key_secret = key_secret
        self._sleep = sleep

    def esa(self, action: str, **params: Any) -> Dict[str, Any]:
        """Call an ESA action; dict/list parameters are sent as JSON strings."""
        return self.call(ESA_ENDPOINT, ESA_VERSION, action, params)

    def call(self, host: str, version: str, action: str, params: Mapping[str, Any]) -> Dict[str, Any]:
        clean = {key: _param_value(value) for key, value in params.items() if value is not None}
        encoded = _encode_params(clean)
        # ESA read actions accept only GET (parameters in the query string);
        # writes take POST with a form body.
        method = "GET" if action.startswith(READ_ACTION_PREFIXES) and action != "VerifySite" else "POST"
        query, body = (encoded, b"") if method == "GET" else ("", encoded.encode("utf-8"))
        last_error: Exception | None = None
        for attempt in range(API_ATTEMPTS):
            headers = {
                "x-acs-action": action,
                "x-acs-version": version,
                "x-acs-date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "x-acs-signature-nonce": secrets_module.token_hex(16),
                "x-acs-content-sha256": hashlib.sha256(body).hexdigest(),
            }
            if method == "POST":
                headers["content-type"] = "application/x-www-form-urlencoded"
            authorization, _ = acs3_signature(
                method=method, host=host, headers=headers, query=query, body=body,
                key_id=self.key_id, key_secret=self.key_secret,
            )
            request = urllib.request.Request(
                f"https://{host}/" + (f"?{query}" if query else ""),
                data=body if method == "POST" else None,
                headers={**headers, "Authorization": authorization, "Accept": "application/json"},
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
                    return _decode_json(response.read(), action)
            except urllib.error.HTTPError as exc:
                payload = _decode_error(exc)
                code = str(payload.get("Code") or f"HTTP{exc.code}")
                error = AliyunApiError(action, int(exc.code), code, str(payload.get("Message") or exc.reason), str(payload.get("RequestId") or ""))
                if not (code.startswith(THROTTLE_CODE_PREFIXES) or (int(exc.code) >= 500 and action.startswith(READ_ACTION_PREFIXES))):
                    raise error from exc
                last_error = error
            except (urllib.error.URLError, OSError, socket.timeout) as exc:
                if not action.startswith(READ_ACTION_PREFIXES):
                    raise LumaError(f"Alibaba Cloud {action} did not answer ({exc}); the next deploy reconciles it") from exc
                last_error = exc
            if attempt + 1 < API_ATTEMPTS:
                self._sleep(0.5 * (2**attempt))
        if isinstance(last_error, AliyunApiError):
            raise last_error
        raise LumaError(f"Alibaba Cloud {action} unavailable after {API_ATTEMPTS} attempts: {last_error}")


def _param_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def _decode_json(raw: bytes, action: str) -> Dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LumaError(f"Alibaba Cloud {action} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise LumaError(f"Alibaba Cloud {action} returned an unexpected response")
    return payload


def _decode_error(exc: urllib.error.HTTPError) -> Dict[str, Any]:
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# ESA operations
# ---------------------------------------------------------------------------


def _wait_until(
    predicate: Callable[[], Optional[Dict[str, Any]]],
    *,
    timeout: float,
    interval: float,
    sleep: Optional[Callable[[float], None]],
    clock: Optional[Callable[[], float]],
) -> Optional[Dict[str, Any]]:
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + timeout
    while True:
        result = predicate()
        if result is not None:
            return result
        if clock() >= deadline:
            return None
        sleep(interval)


def find_site(client: AliyunClient, site_name: str) -> Optional[Dict[str, Any]]:
    result = client.esa("ListSites", SiteName=site_name, SiteSearchType="exact", PageSize=10)
    for item in result.get("Sites") or []:
        if isinstance(item, dict) and str(item.get("SiteName") or "").lower() == site_name.lower():
            return item
    return None


def _plan_instance(client: AliyunClient, scope: str) -> str:
    result = client.esa("ListUserRatePlanInstances", CheckRemainingSiteQuota="true", Status="online", PageSize=100)
    for item in result.get("InstanceInfo") or []:
        if not isinstance(item, dict):
            continue
        coverages = {value.strip() for value in str(item.get("Coverages") or "").split(",") if value.strip()}
        if not coverages or scope in coverages:
            return str(item["InstanceId"])
    raise LumaError(
        f"no ESA plan with a free site slot covering {scope}; buy a plan in the ESA console (one plan serves one root domain)"
    )


def ensure_site(
    client: AliyunClient,
    *,
    site_name: str,
    scope: str,
    publish_txt: Callable[[str, str], str],
    sleep: Optional[Callable[[float], None]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Return an active CNAME-access ESA site for the zone, creating it once."""
    notes: List[str] = []
    site = find_site(client, site_name)
    if site is None:
        created = client.esa(
            "CreateSite",
            SiteName=site_name,
            Coverage=scope,
            AccessType="CNAME",
            InstanceId=_plan_instance(client, scope),
        )
        site = {"SiteId": created["SiteId"], "SiteName": site_name, "Status": "pending", "VerifyCode": created.get("VerifyCode"), "Coverage": scope, "AccessType": "CNAME"}
        notes.append(f"site {site_name} created ({scope})")
    if str(site.get("AccessType") or "CNAME").upper() != "CNAME":
        raise LumaError(f"ESA site {site_name} uses {site.get('AccessType')} access; Luma manages CNAME-access sites only")
    status = str(site.get("Status") or "")
    if status == "offline":
        raise LumaError(f"ESA site {site_name} is offline ({site.get('OfflineReason') or 'see the ESA console'})")
    if status != "active":
        code = str(site.get("VerifyCode") or "").strip()
        if not code:
            raise LumaError(f"ESA site {site_name} is {status} and returned no verification code")
        record = publish_txt(f"_esaauth.{site_name}", code)

        def verified() -> Optional[Dict[str, Any]]:
            return {"ok": True} if client.esa("VerifySite", SiteId=site["SiteId"]).get("Passed") else None

        if _wait_until(verified, timeout=VERIFY_TIMEOUT_SECONDS, interval=VERIFY_POLL_SECONDS, sleep=sleep, clock=clock) is None:
            raise LumaError(f"ESA site {site_name} ownership check did not pass; TXT {record} must hold {code}")
        notes.append(f"site verified via TXT {record}")
        site = {**site, "Status": "active"}
    if str(site.get("Coverage") or scope) != scope:
        notes.append(f"site coverage is {site.get('Coverage')}; manifest asks {scope} (not changed)")
    return site, notes


SITE_DEFAULTS_VERSION = 6
DOMAIN_RULE_SEQUENCE = 100
SITE_RULE_PREFIX = "luma_site_"


def _ensure_global(client: AliyunClient, *, site_id: Any, kind: str, settings: Dict[str, str]) -> Optional[str]:
    """Create or correct the site's single global ``kind`` config."""
    listing = client.esa(f"List{kind}s", SiteId=site_id, ConfigType="global")
    current = next((item for item in listing.get("Configs") or [] if isinstance(item, dict)), None)
    if current is None:
        client.esa(f"Create{kind}", SiteId=site_id, **settings)
        return f"{kind} global created"
    if any(str(current.get(key) or "") != value for key, value in settings.items()):
        client.esa(f"Update{kind}", SiteId=site_id, ConfigId=current["ConfigId"], **settings)
        return f"{kind} global updated"
    return None


def configure_site(client: AliyunClient, site_id: Any) -> List[str]:
    """Site-wide defaults shared by every Luma domain on the site.

    The origin setting is a global config; honouring cache headers on HTML
    needs one site-wide cache rule. Each ``cdn.cache`` path adds one more
    (Basic plans allow 10 cache rules per site).
    """
    applied: List[str] = []
    # Always fetch the origin over HTTPS and verify Traefik's certificate; ESA
    # itself redirects plain HTTP. With "follow", Traefik's HTTP->HTTPS 301
    # could be cached under a key shared with HTTPS and loop.
    for kind, settings in (
        ("OriginRule", {"OriginScheme": "https", "OriginVerify": "on"}),
        ("HttpsApplicationConfiguration", {"HttpsForce": "on", "HttpsForceCode": "301"}),
        # HTTP/2 and HTTP/3 to visitors; ESA's console showed HTTP/2 off.
        ("HttpsBasicConfiguration", {"Http2": "on", "Http3": "on"}),
        # Traefik speaks HTTP/2 over TLS; WebSocket stays on for apps that use it.
        ("NetworkOptimization", {"Http2Origin": "on", "Websocket": "on"}),
        # Each encoding is only used when the visitor's browser accepts it.
        ("CompressionRule", {"Gzip": "on", "Brotli": "on", "Zstd": "on"}),
    ):
        change = _ensure_global(client, site_id=site_id, kind=kind, settings=settings)
        if change:
            applied.append(change)
    # A global cache config only applies to ESA's default static extensions
    # (measured: HTML stayed DYNAMIC), so honouring the application's cache
    # headers on every path, HTML included, takes one site-wide rule.
    applied.extend(_reconcile_rules(
        client,
        site_id=site_id,
        kind="CacheRule",
        prefix=SITE_RULE_PREFIX,
        # cache_all makes HTML eligible at all (without it ESA reports DYNAMIC);
        # follow_origin_bypass then caches only what the application marks
        # cacheable and never guesses from ETag/Last-Modified.
        desired=[{
            "RuleName": SITE_RULE_PREFIX + "origin_headers",
            "Rule": "true",
            "BypassCache": "cache_all",
            "EdgeCacheMode": "follow_origin_bypass",
            "Sequence": "1",
        }],
    ))
    client.esa("UpdateManagedTransform", SiteId=site_id, AddRealClientIpHeader="on")
    applied.append("real client IP header")
    return applied


def _q(value: str) -> str:
    return json.dumps(value)


def _path_condition(path: str) -> str:
    if path.endswith("*"):
        return f"starts_with(http.request.uri.path, {_q(path[:-1])})"
    return f"http.request.uri.path eq {_q(path)}"


def _rule_prefix(domain: str) -> str:
    # The digest keeps a.b.example.com and a-b.example.com apart.
    readable = "".join(ch if ch.isalnum() else "-" for ch in domain.lower())
    digest = hashlib.sha256(domain.lower().encode("utf-8")).hexdigest()[:8]
    return f"luma_{readable}_{digest}_"


def domain_rules(domain: str, cache: Iterable[CachePath]) -> Dict[str, List[Dict[str, Any]]]:
    """Per-domain ESA rules Luma owns, keyed by rule kind.

    Only ``cdn.cache`` paths need rules; everything else uses the site-wide
    rule. Listed paths cannot overlap, and each path rule carries a Sequence
    after the site-wide rule.
    """
    prefix = _rule_prefix(domain)
    host = f"http.host eq {_q(domain)}"
    cache_rules = [
        {
            "RuleName": f"{prefix}cache{index}",
            "Rule": f"({host} and {_path_condition(item.path)})",
            "BypassCache": "cache_all",
            "EdgeCacheMode": "follow_origin_override",
            "EdgeCacheTtl": str(item.ttl),
            # Rules apply top to bottom; path rules run after the site-wide
            # rule (Sequence 1) so their TTL is the one that sticks.
            "Sequence": str(DOMAIN_RULE_SEQUENCE + index),
        }
        for index, item in enumerate(cache, start=1)
    ]
    # OriginRule stays listed with no rules so earlier per-domain rules are removed.
    return {"CacheRule": cache_rules, "OriginRule": []}


def _reconcile_rules(client: AliyunClient, *, site_id: Any, kind: str, prefix: str, desired: List[Dict[str, Any]]) -> List[str]:
    listing = client.esa(f"List{kind}s", SiteId=site_id, ConfigType="rule", PageSize=500)
    existing = {
        str(item.get("RuleName")): item
        for item in listing.get("Configs") or []
        if isinstance(item, dict) and str(item.get("RuleName") or "").startswith(prefix)
    }
    changes: List[str] = []
    for rule in desired:
        current = existing.pop(rule["RuleName"], None)
        if current is None:
            try:
                client.esa(f"Create{kind}", SiteId=site_id, RuleEnable="on", **rule)
            except AliyunApiError as exc:
                if "Exceed" in exc.code or "Quota" in exc.code:
                    raise LumaError(
                        f"ESA {kind} quota reached for this site ({exc.code}); every app on the root domain shares it "
                        "(Free 5, Basic 10, Standard 50). Remove cdn.cache paths or upgrade the ESA plan"
                    ) from exc
                raise
            changes.append(f"{rule['RuleName']} created")
        elif str(current.get("RuleEnable") or "on") != "on" or any(str(current.get(key) or "") != str(value) for key, value in rule.items()):
            client.esa(f"Update{kind}", SiteId=site_id, ConfigId=current["ConfigId"], RuleEnable="on", **rule)
            changes.append(f"{rule['RuleName']} updated")
    for name, current in existing.items():
        client.esa(f"Delete{kind}", SiteId=site_id, ConfigId=current["ConfigId"])
        changes.append(f"{name} deleted")
    return changes


def reconcile_domain_rules(client: AliyunClient, *, site_id: Any, domain: str, cache: Iterable[CachePath]) -> List[str]:
    prefix = _rule_prefix(domain)
    changes: List[str] = []
    for kind, desired in domain_rules(domain, cache).items():
        changes.extend(_reconcile_rules(client, site_id=site_id, kind=kind, prefix=prefix, desired=desired))
    return changes


def delete_domain_rules(client: AliyunClient, *, site_id: Any, domain: str) -> List[str]:
    prefix = _rule_prefix(domain)
    changes: List[str] = []
    for kind in ("CacheRule", "OriginRule"):
        changes.extend(_reconcile_rules(client, site_id=site_id, kind=kind, prefix=prefix, desired=[]))
    return changes


def find_record(client: AliyunClient, site_id: Any, name: str) -> Optional[Dict[str, Any]]:
    result = client.esa("ListRecords", SiteId=site_id, RecordName=name, RecordMatchType="exact", PageSize=50)
    for item in result.get("Records") or []:
        if isinstance(item, dict) and str(item.get("RecordName") or "").lower() == name.lower():
            return item
    return None


def _record_value(record: Mapping[str, Any]) -> str:
    data = record.get("Data") if isinstance(record.get("Data"), Mapping) else {}
    return str(data.get("Value") or data.get("value") or "")


def _record_params(origin: str) -> Dict[str, Any]:
    try:
        ipaddress.ip_address(origin)
        return {"Type": "A/AAAA"}
    except ValueError:
        # A hostname origin: keep the visitor's Host so Traefik routes it.
        return {"Type": "CNAME", "SourceType": "Domain", "HostPolicy": "follow_hostname"}


def ensure_record(client: AliyunClient, *, site_id: Any, domain: str, origin: str) -> Tuple[Dict[str, Any], List[str]]:
    """Make ``domain`` a proxied ESA record pointing at the edge; return it with notes."""
    notes: List[str] = []
    record = find_record(client, site_id, domain)
    data = {"Value": origin}
    params = _record_params(origin)
    if record is None:
        client.esa("CreateRecord", SiteId=site_id, RecordName=domain, Proxied=True, BizName="web", Ttl=1, Data=data, **params)
        notes.append(f"record created -> {origin}")
    elif str(record.get("RecordType") or "").upper() != params["Type"].upper():
        raise LumaError(
            f"ESA record {domain} is {record.get('RecordType')} but the origin needs {params['Type']}; "
            "delete it in the ESA console so Luma can recreate it"
        )
    elif _record_value(record) != origin or not record.get("Proxied"):
        client.esa("UpdateRecord", RecordId=record["RecordId"], Proxied=True, BizName="web", Ttl=1, Data=data, **params)
        notes.append(f"record updated -> {origin}")
    record = find_record(client, site_id, domain)
    if record is None or not record.get("RecordCname"):
        raise LumaError(f"ESA returned no CNAME for {domain}")
    return record, notes


def dcv_delegation_target(client: AliyunClient, *, site_id: Any, domain: str) -> Tuple[str, str]:
    """Return ``(record name, CNAME target)`` delegating Let's Encrypt DCV to ESA.

    With the delegation in place ESA validates over DNS, so the certificate
    is issued before visitors are switched and renewals keep working.
    """
    delegation = str(client.esa("GetDcvDelegation", SiteId=site_id).get("DelegationDomain") or "").strip().rstrip(".")
    if not delegation:
        raise LumaError("ESA returned no DCV delegation domain for the site")
    return f"_acme-challenge.{domain}", f"{domain}.{delegation}"


def certificate_status(client: AliyunClient, *, site_id: Any, domain: str) -> Tuple[str, int]:
    """Return ``(status, valid certificate count)`` for the record."""
    # Without Detail the Certificates list stays empty; Count is always set.
    result = client.esa("ListCertificatesByRecord", SiteId=site_id, RecordName=domain, ValidOnly=True, Detail=True)
    for item in result.get("Result") or []:
        if isinstance(item, dict):
            try:
                count = int(item.get("Count") or 0)
            except (TypeError, ValueError):
                count = 0
            return str(item.get("Status") or "none"), max(count, len(item.get("Certificates") or []))
    return "none", 0


def ensure_certificate(
    client: AliyunClient,
    *,
    site_id: Any,
    domain: str,
    sleep: Optional[Callable[[float], None]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> str:
    """Request ESA's free certificate once DNS points at ESA, and wait for it."""
    status, count = certificate_status(client, site_id=site_id, domain=domain)
    if count:
        return "certificate ready"
    if status != "applying":
        try:
            client.esa("ApplyCertificate", SiteId=site_id, Domains=domain, Type="lets_encrypt")
        except AliyunApiError as exc:
            if exc.code != "InvalidParameter.Domains":  # already applied
                raise

    def issued() -> Optional[Dict[str, Any]]:
        current, valid = certificate_status(client, site_id=site_id, domain=domain)
        if valid:
            return {"ok": True}
        if current == "failed":
            raise LumaError(f"ESA could not issue a certificate for {domain}; check that DNS points at ESA")
        return None

    if _wait_until(issued, timeout=CERTIFICATE_TIMEOUT_SECONDS, interval=CERTIFICATE_POLL_SECONDS, sleep=sleep, clock=clock) is None:
        raise LumaError(f"ESA certificate for {domain} is still being issued after {CERTIFICATE_TIMEOUT_SECONDS // 60} minutes")
    return "certificate issued (Let's Encrypt)"


def purge_esa(client: AliyunClient, *, site_id: Any, domain: str, paths: Optional[Iterable[str]]) -> List[str]:
    task_ids: List[str] = []
    for request in purge_requests(domain, paths):
        result = client.esa("PurgeCaches", SiteId=site_id, **request)
        if result.get("TaskId"):
            task_ids.append(str(result["TaskId"]))
    return task_ids


def delete_esa_record(client: AliyunClient, *, site_id: Any, domain: str) -> bool:
    delete_domain_rules(client, site_id=site_id, domain=domain)
    record = find_record(client, site_id, domain)
    if record is None:
        return False
    client.esa("DeleteRecord", RecordId=record["RecordId"])
    return True
