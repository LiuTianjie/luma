#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${EGRESS_SUBSCRIPTION_URL:-}" ]]; then
  echo "EGRESS_SUBSCRIPTION_URL is required" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="${EGRESS_CONFIG_DIR:-/opt/luma/egress-gateway}"
mkdir -p "$CONFIG_DIR" "$CONFIG_DIR/providers"
chmod 700 "$CONFIG_DIR" "$CONFIG_DIR/providers"

DOWNLOAD_TMP="$CONFIG_DIR/config.download.tmp"
trap 'rm -f "$DOWNLOAD_TMP"' EXIT

curl -fsSL -A "Clash.Meta" "$EGRESS_SUBSCRIPTION_URL" -o "$DOWNLOAD_TMP"
chmod 600 "$DOWNLOAD_TMP"

python3 - "$DOWNLOAD_TMP" "$CONFIG_DIR" "$REPO_ROOT" <<'PY'
import base64
import os
import secrets
import sys
import urllib.parse
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    raise SystemExit("python yaml module is required. Install python3-yaml or PyYAML.") from exc

source = Path(sys.argv[1])
config_dir = Path(sys.argv[2])
repo_root = Path(sys.argv[3])
subscription_url = os.environ["EGRESS_SUBSCRIPTION_URL"].strip()

if (repo_root / "luma" / "egress.py").is_file():
    sys.path.insert(0, str(repo_root))

raw = source.read_bytes()

try:
    from luma.egress import (
        _extract_usable_proxies,
        ensure_mihomo_direct_domains,
        minimal_mihomo_provider_config,
    )
    from luma.errors import LumaError

    try:
        _extract_usable_proxies(raw)
        rendered = minimal_mihomo_provider_config(subscription_url)
    except LumaError as exc:
        raise SystemExit(str(exc)) from exc
except ImportError:
    if not subscription_url or any(ch.isspace() or ord(ch) < 32 for ch in subscription_url):
        raise SystemExit("egress subscription URL must be a valid http or https URL")
    parsed_url = urllib.parse.urlparse(subscription_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise SystemExit("egress subscription URL must use http or https")

    text = raw.decode("utf-8", errors="replace").strip()
    if "proxies:" not in text:
        compact = "".join(text.split())
        try:
            text = base64.b64decode(compact + "=" * (-len(compact) % 4)).decode("utf-8", errors="replace")
        except Exception:
            pass
    if "proxies:" not in text:
        raise SystemExit("subscription did not return a mihomo/clash YAML config")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise SystemExit("subscription YAML must be a mapping")
    raw_proxies = data.get("proxies")
    if not isinstance(raw_proxies, list):
        raise SystemExit("subscription contains no usable proxies")
    proxies = [
        proxy
        for proxy in raw_proxies
        if isinstance(proxy, dict) and proxy.get("name") and proxy.get("type")
    ]
    if not proxies:
        raise SystemExit("subscription contains no usable proxies")

    secret = str(os.environ.get("LUMA_EGRESS_SECRET") or "").strip() or secrets.token_hex(16)
    config = {
        "mixed-port": 7890,
        "allow-lan": True,
        "bind-address": "0.0.0.0",
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "external-controller": "127.0.0.1:9090",
        "secret": secret,
        "dns": {
            "enable": True,
            "listen": "127.0.0.1:1053",
            "ipv6": False,
            "enhanced-mode": "redir-host",
            "nameserver": ["223.5.5.5", "119.29.29.29", "1.1.1.1"],
        },
        "proxy-providers": {
            "egress-sub": {
                "type": "http",
                "url": subscription_url,
                "path": "./providers/egress-sub.yaml",
                "interval": 3600,
                "health-check": {
                    "enable": True,
                    "url": "https://www.gstatic.com/generate_204",
                    "interval": 300,
                    "timeout": 5000,
                    "lazy": True,
                },
            }
        },
        "proxy-groups": [
            {
                "name": "EGRESS-AUTO",
                "type": "url-test",
                "use": ["egress-sub"],
                "url": "https://www.gstatic.com/generate_204",
                "interval": 300,
                "tolerance": 150,
                "lazy": True,
            },
            {
                "name": "EGRESS",
                "type": "fallback",
                "proxies": ["EGRESS-AUTO"],
                "use": ["egress-sub"],
                "url": "https://www.gstatic.com/generate_204",
                "interval": 300,
                "lazy": True,
            },
        ],
        "rules": ["MATCH,EGRESS"],
    }
    rendered = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    ensure_mihomo_direct_domains = None

target = config_dir / "config.yaml"
if target.is_file() and ensure_mihomo_direct_domains is not None:
    try:
        existing = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception:
        existing = None
    if isinstance(existing, dict):
        direct_domains = []
        for rule in existing.get("rules") or []:
            parts = [part.strip() for part in str(rule).split(",")]
            if len(parts) == 3 and parts[0].upper() == "DOMAIN" and parts[2].upper() == "DIRECT":
                direct_domains.append(parts[1])
        if direct_domains:
            rendered, _ = ensure_mihomo_direct_domains(rendered, direct_domains)

def write_private_atomic(path: Path, payload: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

write_private_atomic(config_dir / "providers" / "egress-sub.yaml", raw)
write_private_atomic(config_dir / "config.download", raw)
write_private_atomic(target, rendered.encode("utf-8"))
PY

chmod 600 "$CONFIG_DIR/config.yaml" "$CONFIG_DIR/config.download" "$CONFIG_DIR/providers/egress-sub.yaml"

if command -v nomad >/dev/null 2>&1 && nomad job status egress >/dev/null 2>&1; then
  nomad job restart -yes egress >/dev/null
fi

echo "egress config refreshed: $CONFIG_DIR/config.yaml"
