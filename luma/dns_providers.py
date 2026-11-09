"""Record-level clients for the DNS providers a Luma zone can use.

Cloudflare record writes keep their established implementation in
``luma.cloudflare``; this module adds Alibaba Cloud DNS and Tencent Cloud
DNSPod behind one small interface and the credential/verification helpers
shared by every provider.  Requests are signed with the standard library so
the CLI keeps zero cloud SDK dependencies.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import cloudflare
from .cdn import AliyunClient
from .errors import LumaError

PROVIDER_CLOUDFLARE = "cloudflare"
PROVIDER_ALIDNS = "alidns"
PROVIDER_DNSPOD = "dnspod"

# Credential fields per provider, in prompt order.
PROVIDER_FIELDS: Dict[str, tuple[str, ...]] = {
    PROVIDER_CLOUDFLARE: ("apiToken",),
    PROVIDER_ALIDNS: ("accessKeyId", "accessKeySecret"),
    PROVIDER_DNSPOD: ("secretId", "secretKey"),
}
# The environment variables each vendor's own tooling reads.  The CLI uses them
# when present so a developer does not have to paste a key twice.
PROVIDER_ENV: Dict[str, Dict[str, str]] = {
    PROVIDER_CLOUDFLARE: {"apiToken": "CLOUDFLARE_API_TOKEN"},
    PROVIDER_ALIDNS: {"accessKeyId": "ALIBABA_CLOUD_ACCESS_KEY_ID", "accessKeySecret": "ALIBABA_CLOUD_ACCESS_KEY_SECRET"},
    PROVIDER_DNSPOD: {"secretId": "TENCENTCLOUD_SECRET_ID", "secretKey": "TENCENTCLOUD_SECRET_KEY"},
}
PROVIDER_LABELS = {
    PROVIDER_CLOUDFLARE: "Cloudflare",
    PROVIDER_ALIDNS: "Alibaba Cloud DNS",
    PROVIDER_DNSPOD: "Tencent Cloud DNSPod",
}
FIELD_LABELS = {
    "apiToken": "API token",
    "accessKeyId": "AccessKey ID",
    "accessKeySecret": "AccessKey Secret",
    "secretId": "SecretId",
    "secretKey": "SecretKey",
}
# Free plans of both Chinese providers reject TTLs below ten minutes.
MIN_TTL = {PROVIDER_ALIDNS: 600, PROVIDER_DNSPOD: 600}
DEFAULT_TTL = 600

ALIDNS_ENDPOINT = "alidns.aliyuncs.com"
ALIDNS_VERSION = "2015-01-09"
DNSPOD_ENDPOINT = "dnspod.tencentcloudapi.com"
DNSPOD_SERVICE = "dnspod"
DNSPOD_VERSION = "2021-03-23"
DNSPOD_DEFAULT_LINE = "默认"
API_ATTEMPTS = 4
API_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class DnsRecord:
    id: str
    name: str
    type: str
    content: str
    ttl: int = 0


@dataclass
class ProviderCredentials:
    provider: str
    values: Dict[str, str] = field(default_factory=dict)

    def require(self) -> None:
        missing = [name for name in PROVIDER_FIELDS[self.provider] if not str(self.values.get(name) or "").strip()]
        if missing:
            raise LumaError(f"{PROVIDER_LABELS[self.provider]} credential missing: {', '.join(missing)}")


def normalize_provider(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {"cf": PROVIDER_CLOUDFLARE, "aliyun": PROVIDER_ALIDNS, "alibaba": PROVIDER_ALIDNS, "tencent": PROVIDER_DNSPOD, "tencentcloud": PROVIDER_DNSPOD}
    provider = aliases.get(raw, raw)
    if provider not in PROVIDER_FIELDS:
        raise LumaError(f"DNS provider must be one of {', '.join(sorted(PROVIDER_FIELDS))}")
    return provider


def relative_name(name: str, zone: str) -> str:
    """Return the record host relative to ``zone`` ("@" for the apex)."""
    clean = str(name or "").strip().strip(".").lower()
    apex = str(zone or "").strip().strip(".").lower()
    if clean == apex:
        return "@"
    if not clean.endswith(f".{apex}"):
        raise LumaError(f"{clean} is not inside DNS zone {apex}")
    return clean[: -len(apex) - 1]


def provider_ttl(provider: str, ttl: Any) -> int:
    try:
        value = int(ttl)
    except (TypeError, ValueError):
        value = DEFAULT_TTL
    if value <= 1:
        # Cloudflare's "automatic" TTL has no equivalent elsewhere.
        value = DEFAULT_TTL
    return max(value, MIN_TTL.get(provider, 1))


class RecordProvider:
    """Minimal record API used by the generic synchronizer in ``luma.dns``."""

    provider = ""

    def __init__(self, zone: str):
        self.zone = str(zone).strip().strip(".").lower()

    def records(self, name: str, record_type: str = "") -> List[DnsRecord]:  # pragma: no cover - interface
        raise NotImplementedError

    def create(self, name: str, record_type: str, content: str, ttl: int) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def update(self, record: DnsRecord, name: str, record_type: str, content: str, ttl: int) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def delete(self, record: DnsRecord) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def verify(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Alibaba Cloud DNS
# ---------------------------------------------------------------------------


class AlidnsProvider(RecordProvider):
    provider = PROVIDER_ALIDNS

    def __init__(self, zone: str, key_id: str, key_secret: str, *, client: Optional[AliyunClient] = None):
        super().__init__(zone)
        self.client = client or AliyunClient(key_id, key_secret)

    def _call(self, action: str, **params: Any) -> Dict[str, Any]:
        return self.client.call(ALIDNS_ENDPOINT, ALIDNS_VERSION, action, params)

    def records(self, name: str, record_type: str = "") -> List[DnsRecord]:
        fqdn = str(name).strip().strip(".").lower()
        params: Dict[str, Any] = {"DomainName": self.zone, "SubDomain": fqdn, "PageSize": 100}
        if record_type:
            params["Type"] = record_type.upper()
        payload = self._call("DescribeSubDomainRecords", **params)
        items = ((payload.get("DomainRecords") or {}).get("Record")) or []
        out: List[DnsRecord] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            rr = str(item.get("RR") or "@")
            host = self.zone if rr == "@" else f"{rr}.{self.zone}"
            if host != fqdn:
                continue
            out.append(DnsRecord(
                id=str(item.get("RecordId") or ""),
                name=host,
                type=str(item.get("Type") or "").upper(),
                content=str(item.get("Value") or ""),
                ttl=int(item.get("TTL") or 0),
            ))
        return out

    def create(self, name: str, record_type: str, content: str, ttl: int) -> None:
        self._call(
            "AddDomainRecord",
            DomainName=self.zone,
            RR=relative_name(name, self.zone),
            Type=record_type.upper(),
            Value=content,
            TTL=provider_ttl(self.provider, ttl),
        )

    def update(self, record: DnsRecord, name: str, record_type: str, content: str, ttl: int) -> None:
        if record.type == record_type.upper() and record.content.rstrip(".") == content.rstrip(".") and record.ttl == provider_ttl(self.provider, ttl):
            # Alibaba Cloud rejects an update that changes nothing.
            return
        self._call(
            "UpdateDomainRecord",
            RecordId=record.id,
            RR=relative_name(name, self.zone),
            Type=record_type.upper(),
            Value=content,
            TTL=provider_ttl(self.provider, ttl),
        )

    def delete(self, record: DnsRecord) -> None:
        self._call("DeleteDomainRecord", RecordId=record.id)

    def verify(self) -> str:
        payload = self._call("DescribeDomainInfo", DomainName=self.zone)
        name = str(payload.get("DomainName") or self.zone)
        self._call("DescribeDomainRecords", DomainName=self.zone, PageSize=1)
        return f"Alibaba Cloud DNS zone {name} is reachable"


# ---------------------------------------------------------------------------
# Tencent Cloud DNSPod (API 3.0, TC3-HMAC-SHA256)
# ---------------------------------------------------------------------------


def tc3_authorization(
    *,
    secret_id: str,
    secret_key: str,
    service: str,
    host: str,
    action: str,
    payload: bytes,
    timestamp: int,
) -> str:
    """Return the TC3-HMAC-SHA256 ``Authorization`` header for a JSON POST."""
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{host}\nx-tc-action:{action.lower()}\n"
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = "\n".join([
        "POST",
        "/",
        "",
        canonical_headers,
        signed_headers,
        hashlib.sha256(payload).hexdigest(),
    ])
    scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256",
        str(timestamp),
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    def sign(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    secret_date = sign(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = sign(secret_date, service)
    secret_signing = sign(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"


class TencentApiError(LumaError):
    def __init__(self, action: str, code: str, message: str, request_id: str = ""):
        self.action = action
        self.code = code
        self.request_id = request_id
        suffix = f" (RequestId {request_id})" if request_id else ""
        super().__init__(f"Tencent Cloud {action} failed: {code}: {message}{suffix}")


class TencentClient:
    def __init__(self, secret_id: str, secret_key: str, *, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self.secret_id = secret_id
        self.secret_key = secret_key
        self._sleep = sleep
        self._clock = clock

    def call(self, action: str, params: Mapping[str, Any]) -> Dict[str, Any]:
        body = json.dumps({key: value for key, value in params.items() if value is not None}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        read = action.startswith("Describe")
        last_error: Exception | None = None
        for attempt in range(API_ATTEMPTS):
            timestamp = int(self._clock())
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "Host": DNSPOD_ENDPOINT,
                "X-TC-Action": action,
                "X-TC-Version": DNSPOD_VERSION,
                "X-TC-Timestamp": str(timestamp),
                "Authorization": tc3_authorization(
                    secret_id=self.secret_id,
                    secret_key=self.secret_key,
                    service=DNSPOD_SERVICE,
                    host=DNSPOD_ENDPOINT,
                    action=action,
                    payload=body,
                    timestamp=timestamp,
                ),
            }
            request = urllib.request.Request(f"https://{DNSPOD_ENDPOINT}/", data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
                    payload = json.loads(response.read().decode("utf-8") or "{}")
            except (urllib.error.URLError, OSError, socket.timeout, json.JSONDecodeError) as exc:
                if not read:
                    raise LumaError(f"Tencent Cloud {action} did not answer ({exc}); the next deploy reconciles it") from exc
                last_error = exc
            else:
                result = payload.get("Response") if isinstance(payload, dict) else None
                if not isinstance(result, dict):
                    raise LumaError(f"Tencent Cloud {action} returned an unexpected response")
                error = result.get("Error")
                if not isinstance(error, dict):
                    return result
                code = str(error.get("Code") or "Unknown")
                api_error = TencentApiError(action, code, str(error.get("Message") or ""), str(result.get("RequestId") or ""))
                if not code.startswith(("RequestLimitExceeded", "InternalError")):
                    raise api_error
                last_error = api_error
            if attempt + 1 < API_ATTEMPTS:
                self._sleep(0.5 * (2**attempt))
        if isinstance(last_error, TencentApiError):
            raise last_error
        raise LumaError(f"Tencent Cloud {action} unavailable after {API_ATTEMPTS} attempts: {last_error}")


class DnspodProvider(RecordProvider):
    provider = PROVIDER_DNSPOD

    def __init__(self, zone: str, secret_id: str, secret_key: str, *, client: Optional[TencentClient] = None):
        super().__init__(zone)
        self.client = client or TencentClient(secret_id, secret_key)

    def records(self, name: str, record_type: str = "") -> List[DnsRecord]:
        fqdn = str(name).strip().strip(".").lower()
        subdomain = relative_name(fqdn, self.zone)
        params: Dict[str, Any] = {"Domain": self.zone, "Subdomain": subdomain, "Limit": 100}
        if record_type:
            params["RecordType"] = record_type.upper()
        try:
            payload = self.client.call("DescribeRecordList", params)
        except TencentApiError as exc:
            # DNSPod reports an empty result as an error.
            if exc.code == "ResourceNotFound.NoDataOfRecord":
                return []
            raise
        out: List[DnsRecord] = []
        for item in payload.get("RecordList") or []:
            if not isinstance(item, dict) or str(item.get("Name") or "") != subdomain:
                continue
            out.append(DnsRecord(
                id=str(item.get("RecordId") or ""),
                name=fqdn,
                type=str(item.get("Type") or "").upper(),
                content=str(item.get("Value") or ""),
                ttl=int(item.get("TTL") or 0),
            ))
        return out

    def create(self, name: str, record_type: str, content: str, ttl: int) -> None:
        self.client.call("CreateRecord", {
            "Domain": self.zone,
            "SubDomain": relative_name(name, self.zone),
            "RecordType": record_type.upper(),
            "RecordLine": DNSPOD_DEFAULT_LINE,
            "Value": content,
            "TTL": provider_ttl(self.provider, ttl),
        })

    def update(self, record: DnsRecord, name: str, record_type: str, content: str, ttl: int) -> None:
        if record.type == record_type.upper() and record.content.rstrip(".") == content.rstrip(".") and record.ttl == provider_ttl(self.provider, ttl):
            return
        self.client.call("ModifyRecord", {
            "Domain": self.zone,
            "RecordId": int(record.id),
            "SubDomain": relative_name(name, self.zone),
            "RecordType": record_type.upper(),
            "RecordLine": DNSPOD_DEFAULT_LINE,
            "Value": content,
            "TTL": provider_ttl(self.provider, ttl),
        })

    def delete(self, record: DnsRecord) -> None:
        self.client.call("DeleteRecord", {"Domain": self.zone, "RecordId": int(record.id)})

    def verify(self) -> str:
        payload = self.client.call("DescribeDomain", {"Domain": self.zone})
        info = payload.get("DomainInfo") if isinstance(payload.get("DomainInfo"), dict) else {}
        status = str(info.get("Status") or "")
        if status and status.upper() not in {"ENABLE", "ENABLED"}:
            raise LumaError(f"DNSPod zone {self.zone} is {status}")
        return f"DNSPod zone {self.zone} is reachable"


# ---------------------------------------------------------------------------
# Cloudflare (verification and zone lookup only)
# ---------------------------------------------------------------------------


def cloudflare_zone_id(token: str, zone: str) -> str:
    client = cloudflare.CloudflareClient(token)
    query = urllib.parse.urlencode({"name": zone})
    result = client.request("GET", f"/zones?{query}")["result"]
    if not result:
        raise LumaError(f"Cloudflare zone not found for this token: {zone}")
    return str(result[0].get("id") or "")


def verify_cloudflare(token: str, zone_id: str, zone: str) -> str:
    client = cloudflare.CloudflareClient(token)
    client.request("GET", f"/zones/{urllib.parse.quote(zone_id, safe='')}/dns_records?per_page=1")
    return f"Cloudflare zone {zone} is reachable"


def record_provider(provider: str, zone: str, credentials: Mapping[str, str]) -> RecordProvider:
    values = ProviderCredentials(provider, {key: str(value or "") for key, value in credentials.items()})
    values.require()
    if provider == PROVIDER_ALIDNS:
        return AlidnsProvider(zone, values.values["accessKeyId"], values.values["accessKeySecret"])
    if provider == PROVIDER_DNSPOD:
        return DnspodProvider(zone, values.values["secretId"], values.values["secretKey"])
    raise LumaError(f"{provider} records are written through the Cloudflare client")
