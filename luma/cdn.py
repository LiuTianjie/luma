"""Alibaba Cloud ESA (Edge Security Acceleration) integration for Luma services.

A Luma zone is one ESA site joined by CNAME. For each service with ``cdn: esa``
Luma creates a proxied ESA record pointing at the edge, points the Cloudflare
DNS record at the ESA CNAME, lets ESA issue its free certificate and refreshes
the cache after each rollout. Caching policy is left to the application's own
``Cache-Control`` headers.

Requests are signed with ACS3-HMAC-SHA256 using the standard library, so the
package keeps no Alibaba Cloud SDK dependency.
"""

from __future__ import annotations

import hashlib
import hmac
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
CDN_FIELDS = frozenset({"provider", "scope", "purge"})

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
CERTIFICATE_TIMEOUT_SECONDS = 600
CERTIFICATE_POLL_SECONDS = 15.0
RETRYABLE_CODE_PREFIXES = ("Throttling", "ServiceUnavailable", "InternalError", "InternalException", "TooManyRequests", "Site.ServiceBusy", "Record.ServiceBusy", "Record.Reserved")


# ---------------------------------------------------------------------------
# Manifest block
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CdnSpec:
    provider: str = "esa"
    scope: str = "global"
    # None refreshes the whole hostname after each deploy; () disables refresh.
    purge: Optional[Tuple[str, ...]] = None


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
    if raw.get("purge") is None:
        return CdnSpec(provider=provider, scope=scope, purge=None)
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
    return CdnSpec(provider=provider, scope=scope, purge=tuple(purge))


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
        body = _encode_params(clean).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(API_ATTEMPTS):
            headers = {
                "x-acs-action": action,
                "x-acs-version": version,
                "x-acs-date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "x-acs-signature-nonce": secrets_module.token_hex(16),
                "x-acs-content-sha256": hashlib.sha256(body).hexdigest(),
                "content-type": "application/x-www-form-urlencoded",
            }
            authorization, _ = acs3_signature(
                method="POST", host=host, headers=headers, query="", body=body,
                key_id=self.key_id, key_secret=self.key_secret,
            )
            request = urllib.request.Request(
                f"https://{host}/",
                data=body,
                headers={**headers, "Authorization": authorization, "Accept": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
                    return _decode_json(response.read(), action)
            except urllib.error.HTTPError as exc:
                payload = _decode_error(exc)
                code = str(payload.get("Code") or f"HTTP{exc.code}")
                error = AliyunApiError(action, int(exc.code), code, str(payload.get("Message") or exc.reason), str(payload.get("RequestId") or ""))
                if int(exc.code) < 500 and not code.startswith(RETRYABLE_CODE_PREFIXES):
                    raise error from exc
                last_error = error
            except (urllib.error.URLError, OSError, socket.timeout) as exc:
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
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> Optional[Dict[str, Any]]:
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
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
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


def configure_site(client: AliyunClient, site_id: Any) -> List[str]:
    """Site-wide defaults, applied once. Existing global configs are left alone."""
    applied: List[str] = []
    # follow: HTTPS requests fetch over HTTPS, plain HTTP gets Traefik's redirect.
    # An http origin would loop against Traefik's HTTP->HTTPS redirect, so an
    # existing global rule is corrected rather than left alone.
    origin = [item for item in client.esa("ListOriginRules", SiteId=site_id, ConfigType="global").get("Configs") or [] if isinstance(item, dict)]
    if not origin:
        client.esa("CreateOriginRule", SiteId=site_id, OriginScheme="follow")
        applied.append("origin follow")
    elif str(origin[0].get("OriginScheme") or "") not in {"follow", "https"}:
        client.esa("UpdateOriginRule", SiteId=site_id, ConfigId=origin[0]["ConfigId"], OriginScheme="follow")
        applied.append("origin follow (was http)")
    compression = client.esa("ListCompressionRules", SiteId=site_id, ConfigType="global")
    if not compression.get("Configs"):
        client.esa("CreateCompressionRule", SiteId=site_id, Gzip="on", Brotli="on")
        applied.append("gzip+brotli")
    client.esa("UpdateManagedTransform", SiteId=site_id, AddRealClientIpHeader="on")
    applied.append("real client IP header")
    return applied


def find_record(client: AliyunClient, site_id: Any, name: str) -> Optional[Dict[str, Any]]:
    result = client.esa("ListRecords", SiteId=site_id, RecordName=name, RecordMatchType="exact", PageSize=50)
    for item in result.get("Records") or []:
        if isinstance(item, dict) and str(item.get("RecordName") or "").lower() == name.lower():
            return item
    return None


def _record_value(record: Mapping[str, Any]) -> str:
    data = record.get("Data") if isinstance(record.get("Data"), Mapping) else {}
    return str(data.get("Value") or data.get("value") or "")


def ensure_record(client: AliyunClient, *, site_id: Any, domain: str, origin: str) -> Tuple[Dict[str, Any], List[str]]:
    """Make ``domain`` a proxied ESA record pointing at the edge; return it with notes."""
    notes: List[str] = []
    record = find_record(client, site_id, domain)
    data = {"value": origin}
    if record is None:
        client.esa("CreateRecord", SiteId=site_id, RecordName=domain, Type="A/AAAA", Proxied=True, BizName="web", Ttl=1, Data=data)
        notes.append(f"record created -> {origin}")
    elif str(record.get("RecordType") or "").upper() not in {"A/AAAA", "A", "AAAA"}:
        raise LumaError(f"ESA record {domain} is {record.get('RecordType')}; delete it in the ESA console so Luma can manage it")
    elif _record_value(record) != origin or not record.get("Proxied"):
        client.esa("UpdateRecord", RecordId=record["RecordId"], Type="A/AAAA", Proxied=True, BizName="web", Ttl=1, Data=data)
        notes.append(f"record updated -> {origin}")
    record = find_record(client, site_id, domain)
    if record is None or not record.get("RecordCname"):
        raise LumaError(f"ESA returned no CNAME for {domain}")
    return record, notes


def certificate_status(client: AliyunClient, *, site_id: Any, domain: str) -> Tuple[str, int]:
    """Return ``(status, valid certificate count)`` for the record."""
    result = client.esa("ListCertificatesByRecord", SiteId=site_id, RecordName=domain, ValidOnly=True)
    for item in result.get("Result") or []:
        if isinstance(item, dict):
            return str(item.get("Status") or "none"), len(item.get("Certificates") or [])
    return "none", 0


def ensure_certificate(
    client: AliyunClient,
    *,
    site_id: Any,
    domain: str,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
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
    record = find_record(client, site_id, domain)
    if record is None:
        return False
    client.esa("DeleteRecord", RecordId=record["RecordId"])
    return True
