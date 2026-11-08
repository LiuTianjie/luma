"""Alibaba Cloud CDN integration for Luma services.

Luma owns the whole lifecycle of a service's CDN domain: it creates the
accelerated domain (proving ownership through the Cloudflare zone Luma already
manages), installs the Traefik-issued certificate, points DNS at the CDN CNAME
and refreshes entry URLs after each rollout.  Caching policy itself is left to
the application's own ``Cache-Control`` headers; Luma only configures the
origin and HTTPS and adds no cache rules, so the origin headers decide.

The client signs requests with ACS3-HMAC-SHA256 using the standard library so
the package keeps no Alibaba Cloud SDK dependency.
"""

from __future__ import annotations

import base64
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
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import LumaError

CDN_PROVIDERS = frozenset({"aliyun"})
CDN_SCOPES = frozenset({"global", "domestic", "overseas"})
CDN_EXPOSURES = frozenset({"cn-edge", "external-edge", "tailscale-relay"})
CDN_FIELDS = frozenset({"provider", "scope", "purge"})
DEFAULT_PURGE_PATHS: Tuple[str, ...] = ("/",)

ALIYUN_KEY_ID_ENV = "ALIBABA_CLOUD_ACCESS_KEY_ID"
ALIYUN_KEY_SECRET_ENV = "ALIBABA_CLOUD_ACCESS_KEY_SECRET"
ALIYUN_SECRET_NAMES = (ALIYUN_KEY_ID_ENV, ALIYUN_KEY_SECRET_ENV)

CDN_ENDPOINT = "cdn.aliyuncs.com"
CDN_VERSION = "2018-05-10"
CAS_ENDPOINT = "cas.aliyuncs.com"
CAS_VERSION = "2020-04-07"

TRAEFIK_ACME_FILE_ENV = "LUMA_TRAEFIK_ACME_FILE"
DEFAULT_TRAEFIK_ACME_FILE = "/run/luma/traefik-acme/acme.json"

API_ATTEMPTS = 4
API_TIMEOUT_SECONDS = 20
ONLINE_TIMEOUT_SECONDS = 600
ONLINE_POLL_SECONDS = 10.0
VERIFY_TIMEOUT_SECONDS = 180
VERIFY_POLL_SECONDS = 10.0
RETRYABLE_CODE_PREFIXES = ("Throttling", "ServiceUnavailable", "InternalError", "TooManyRequests")


# ---------------------------------------------------------------------------
# Manifest block
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CdnSpec:
    provider: str = "aliyun"
    scope: str = "global"
    purge: Tuple[str, ...] = DEFAULT_PURGE_PATHS


def parse_cdn_block(raw: Any, *, label: str = "cdn") -> Optional[CdnSpec]:
    """Parse ``cdn: aliyun`` or the mapping form; ``None``/``false`` disable it."""
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
    purge_raw = raw.get("purge", list(DEFAULT_PURGE_PATHS))
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


def purge_targets(domain: str, paths: Iterable[str]) -> Dict[str, List[str]]:
    """Split manifest purge paths into Aliyun File and Directory objects."""
    files: List[str] = []
    directories: List[str] = []
    for path in paths:
        if path.endswith("/*"):
            directories.append(f"https://{domain}{path[:-1]}")
        else:
            files.append(f"https://{domain}{path}")
    result: Dict[str, List[str]] = {}
    if files:
        result["File"] = files
    if directories:
        result["Directory"] = directories
    return result


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def aliyun_credentials(state: Mapping[str, Any]) -> Tuple[str, str]:
    stored = state.get("secrets") if isinstance(state.get("secrets"), Mapping) else {}
    key_id = str(stored.get(ALIYUN_KEY_ID_ENV) or os.environ.get(ALIYUN_KEY_ID_ENV) or "").strip()
    key_secret = str(stored.get(ALIYUN_KEY_SECRET_ENV) or os.environ.get(ALIYUN_KEY_SECRET_ENV) or "").strip()
    missing = [name for name, value in ((ALIYUN_KEY_ID_ENV, key_id), (ALIYUN_KEY_SECRET_ENV, key_secret)) if not value]
    if missing:
        raise LumaError(f"missing Alibaba Cloud CDN credential(s): {', '.join(missing)}; set them with luma secret set")
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

    def cdn(self, action: str, **params: Any) -> Dict[str, Any]:
        return self.call(CDN_ENDPOINT, CDN_VERSION, action, params)

    def cas(self, action: str, **params: Any) -> Dict[str, Any]:
        return self.call(CAS_ENDPOINT, CAS_VERSION, action, params)

    def call(self, host: str, version: str, action: str, params: Mapping[str, Any]) -> Dict[str, Any]:
        clean = {key: (str(value).lower() if isinstance(value, bool) else str(value)) for key, value in params.items() if value is not None}
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
# Traefik certificate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EdgeCertificate:
    cert_pem: str
    key_pem: str
    sha256: str
    not_after: int
    names: Tuple[str, ...]


def _name_covers(name: str, domain: str) -> bool:
    name = name.lower().rstrip(".")
    domain = domain.lower().rstrip(".")
    if name == domain:
        return True
    if name.startswith("*."):
        suffix = name[1:]
        return domain.endswith(suffix) and "." not in domain[: -len(suffix)]
    return False


def load_edge_certificate(domain: str, *, resolver: str, path: Optional[Path] = None) -> EdgeCertificate:
    """Find the newest Traefik ACME certificate covering ``domain``."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    acme_path = Path(path or os.environ.get(TRAEFIK_ACME_FILE_ENV) or DEFAULT_TRAEFIK_ACME_FILE)
    try:
        data = json.loads(acme_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LumaError(
            f"Traefik certificate store not found at {acme_path}; CDN HTTPS needs Luma Control on the ingress node "
            "with the Traefik letsencrypt volume mounted (run luma update)"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LumaError(f"cannot read Traefik certificate store {acme_path}: {exc}") from exc
    resolver_data = data.get(resolver) if isinstance(data, dict) else None
    entries = resolver_data.get("Certificates") if isinstance(resolver_data, dict) else None
    best: Optional[EdgeCertificate] = None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        info = entry.get("domain") if isinstance(entry.get("domain"), dict) else {}
        names = tuple(str(item) for item in [info.get("main"), *(info.get("sans") or [])] if item)
        if not any(_name_covers(name, domain) for name in names):
            continue
        try:
            cert_pem = base64.b64decode(str(entry.get("certificate") or "")).decode("utf-8")
            key_pem = base64.b64decode(str(entry.get("key") or "")).decode("utf-8")
            leaf = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        not_after = getattr(leaf, "not_valid_after_utc", None) or leaf.not_valid_after.replace(tzinfo=timezone.utc)
        candidate = EdgeCertificate(
            cert_pem=cert_pem,
            key_pem=key_pem,
            sha256=leaf.fingerprint(hashes.SHA256()).hex(),
            not_after=int(not_after.timestamp()),
            names=names,
        )
        if best is None or candidate.not_after > best.not_after:
            best = candidate
    if best is None:
        raise LumaError(f"no Traefik certificate covers {domain} in resolver {resolver}")
    if best.not_after <= int(time.time()):
        raise LumaError(f"Traefik certificate for {domain} expired; check Traefik ACME renewal")
    return best


# ---------------------------------------------------------------------------
# Aliyun CDN operations
# ---------------------------------------------------------------------------


def _domain_record(client: AliyunClient, domain: str) -> Optional[Dict[str, Any]]:
    result = client.cdn("DescribeUserDomains", DomainName=domain, DomainSearchType="full_match", CheckDomainShow=True)
    pages = result.get("Domains") if isinstance(result.get("Domains"), dict) else {}
    for item in pages.get("PageData") or []:
        if isinstance(item, dict) and str(item.get("DomainName") or "").lower() == domain.lower():
            return item
    return None


def _origin_source(origin: str) -> str:
    try:
        ipaddress.ip_address(origin)
        kind = "ipaddr"
    except ValueError:
        kind = "domain"
    return json.dumps([{"content": origin, "type": kind, "port": 443, "priority": "20", "weight": "10"}])


def baseline_functions(domain: str) -> List[Dict[str, Any]]:
    """Origin and HTTPS settings Luma owns on every CDN domain."""
    def fn(name: str, **args: Any) -> Dict[str, Any]:
        return {"functionName": name, "functionArgs": [{"argName": key, "argValue": str(value)} for key, value in args.items()]}

    return [
        fn("set_req_host_header", domain_name=domain),
        fn("forward_scheme", enable="on", scheme_origin="https", scheme_origin_port="443"),
        fn("https_origin_sni", enabled="on", https_origin_sni=domain),
        fn("https_force", enable="on", https_rewrite="301"),
    ]
    # No cache rules: with none configured, Alibaba Cloud follows the origin's
    # cache headers and does not cache responses that send none.


def _configure_baseline(client: AliyunClient, domain: str) -> List[str]:
    existing = client.cdn("DescribeCdnDomainConfigs", DomainName=domain)
    configs = existing.get("DomainConfigs") if isinstance(existing.get("DomainConfigs"), dict) else {}
    present = {str(item.get("FunctionName")) for item in configs.get("DomainConfig") or [] if isinstance(item, dict)}
    missing = [function for function in baseline_functions(domain) if function["functionName"] not in present]
    if not missing:
        return []
    # One call: each config change puts the domain into "configuring" again.
    try:
        client.cdn("BatchSetCdnDomainConfig", DomainNames=domain, Functions=json.dumps(missing))
    except AliyunApiError as exc:
        if exc.code != "ConfigDuplicate":
            raise
    return [function["functionName"] for function in missing]


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


def _verify_ownership(
    client: AliyunClient,
    domain: str,
    publish_txt: Callable[[str, str], str],
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> str:
    content = str(client.cdn("DescribeVerifyContent", DomainName=domain).get("Content") or "").strip()
    if not content:
        raise LumaError(f"Alibaba Cloud returned no ownership verification content for {domain}")
    record = publish_txt("verification", content)
    last: Optional[AliyunApiError] = None

    def attempt() -> Optional[Dict[str, Any]]:
        nonlocal last
        try:
            client.cdn("VerifyDomainOwner", DomainName=domain, VerifyType="dnsCheck")
            return {"verified": True}
        except AliyunApiError as exc:
            if exc.code != "DomainOwnerVerifyFail":
                raise
            last = exc
            return None

    if _wait_until(attempt, timeout=VERIFY_TIMEOUT_SECONDS, interval=VERIFY_POLL_SECONDS, sleep=sleep, clock=clock) is None:
        raise last or LumaError(f"Alibaba Cloud ownership verification did not pass for {domain}")
    return record


def _ensure_certificate(
    client: AliyunClient,
    domain: str,
    certificate: EdgeCertificate,
    known: Mapping[str, Any],
) -> Tuple[int, bool]:
    """Install ``certificate`` on ``domain``; return ``(cert id, changed)``."""
    if str(known.get("certSha256") or "") == certificate.sha256 and known.get("certId"):
        return int(known["certId"]), False
    name = f"luma-{certificate.sha256[:20]}"
    cert_id: Optional[int] = None
    common_name = certificate.names[0].lstrip("*.") if certificate.names else domain
    listing = client.cas("ListUserCertificateOrder", OrderType="UPLOAD", Keyword=common_name, ShowSize=100)
    for item in listing.get("CertificateOrderList") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("Name") or "") == name or str(item.get("Sha2") or "").lower() == certificate.sha256:
            cert_id = int(item.get("CertificateId"))
            break
    if cert_id is None:
        uploaded = client.cas("UploadUserCertificate", Name=name, Cert=certificate.cert_pem, Key=certificate.key_pem)
        cert_id = int(uploaded.get("CertId"))
    client.cdn("SetCdnDomainSSLCertificate", DomainName=domain, SSLProtocol="on", CertType="cas", CertId=cert_id)
    return cert_id, True


def prepare_aliyun_domain(
    client: AliyunClient,
    *,
    domain: str,
    scope: str,
    origin: str,
    certificate: EdgeCertificate,
    known: Mapping[str, Any],
    publish_txt: Callable[[str, str], str],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    """Create or reconcile an accelerated domain; return the new state record."""
    notes: List[str] = []
    detail = _domain_record(client, domain)
    created = False
    if detail is None:
        params = dict(DomainName=domain, CdnType="web", Scope=scope, Sources=_origin_source(origin))
        try:
            client.cdn("AddCdnDomain", **params)
        except AliyunApiError as exc:
            if exc.code != "DomainOwnerVerifyFail":
                raise
            notes.append(f"ownership verified via TXT {_verify_ownership(client, domain, publish_txt, sleep=sleep, clock=clock)}")
            client.cdn("AddCdnDomain", **params)
        created = True
        notes.append(f"created ({scope})")
    elif str(detail.get("Coverage") or scope) != scope:
        notes.append(f"scope is {detail.get('Coverage')} in Alibaba Cloud; manifest asks {scope} (not changed)")

    def online() -> Optional[Dict[str, Any]]:
        current = _domain_record(client, domain)
        status = str((current or {}).get("DomainStatus") or "")
        if status in {"configure_failed", "check_failed", "offline"}:
            raise LumaError(f"Alibaba Cloud CDN domain {domain} is {status}; check it in the CDN console")
        return current if status == "online" and current.get("Cname") else None

    def wait_online() -> Dict[str, Any]:
        current = _wait_until(online, timeout=ONLINE_TIMEOUT_SECONDS, interval=ONLINE_POLL_SECONDS, sleep=sleep, clock=clock)
        if current is None:
            raise LumaError(f"Alibaba Cloud CDN domain {domain} did not come online within {ONLINE_TIMEOUT_SECONDS // 60} minutes")
        return current

    # Certificate first: forcing HTTPS needs it, and Alibaba Cloud only accepts
    # changes while the domain is online, so wait again after each change.
    detail = wait_online()
    cert_id, cert_changed = _ensure_certificate(client, domain, certificate, known)
    if cert_changed:
        notes.append(f"certificate {certificate.sha256[:12]} installed")
    if created or not known.get("configured"):
        if cert_changed:
            detail = wait_online()
        applied = _configure_baseline(client, domain)
        if applied:
            notes.append("configured " + ", ".join(applied))
    return {
        "provider": "aliyun",
        "cname": str(detail["Cname"]),
        "scope": str(detail.get("Coverage") or scope),
        "origin": origin,
        "configured": True,
        "certId": cert_id,
        "certSha256": certificate.sha256,
        "certNotAfter": certificate.not_after,
        "created": created,
        "notes": notes,
    }


def sync_aliyun_certificate(
    client: AliyunClient, *, domain: str, certificate: EdgeCertificate, known: Mapping[str, Any]
) -> Tuple[int, bool]:
    return _ensure_certificate(client, domain, certificate, known)


def purge_aliyun(client: AliyunClient, *, domain: str, paths: Iterable[str]) -> List[str]:
    task_ids: List[str] = []
    for object_type, urls in purge_targets(domain, paths).items():
        result = client.cdn("RefreshObjectCaches", ObjectPath="\n".join(urls), ObjectType=object_type)
        task_ids.extend(item for item in str(result.get("RefreshTaskId") or "").split(",") if item)
    return task_ids


def delete_aliyun_domain(client: AliyunClient, *, domain: str) -> bool:
    if _domain_record(client, domain) is None:
        return False
    client.cdn("DeleteCdnDomain", DomainName=domain)
    return True
