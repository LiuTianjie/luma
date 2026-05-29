#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${EGRESS_SUBSCRIPTION_URL:-}" ]]; then
  echo "EGRESS_SUBSCRIPTION_URL is required" >&2
  exit 1
fi

CONFIG_DIR="${EGRESS_CONFIG_DIR:-/opt/luma/egress-gateway}"
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

curl -fsSL -A "Clash.Meta" "$EGRESS_SUBSCRIPTION_URL" -o "$CONFIG_DIR/config.download"

python3 - "$CONFIG_DIR/config.download" "$CONFIG_DIR/config.yaml" <<'PY'
from pathlib import Path
import base64
import sys

try:
    import yaml
except ImportError as exc:
    raise SystemExit("python yaml module is required. Install python3-yaml or PyYAML.") from exc

source = Path(sys.argv[1])
target = Path(sys.argv[2])
raw = source.read_bytes()
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

proxies = [
    proxy
    for proxy in data.get("proxies", [])
    if isinstance(proxy, dict) and proxy.get("name") and proxy.get("type")
]
if not proxies:
    raise SystemExit("subscription contains no usable proxies")

proxy_names = [proxy["name"] for proxy in proxies]
config = {
    "mixed-port": 7890,
    "allow-lan": True,
    "bind-address": "0.0.0.0",
    "mode": "global",
    "log-level": "info",
    "ipv6": False,
    "external-controller": "127.0.0.1:9090",
    "dns": {
        "enable": True,
        "listen": "0.0.0.0:1053",
        "ipv6": False,
        "enhanced-mode": "redir-host",
        "nameserver": ["223.5.5.5", "119.29.29.29", "1.1.1.1"],
    },
    "proxies": proxies,
    "proxy-groups": [
        {
            "name": "EGRESS",
            "type": "select",
            "proxies": proxy_names,
        }
    ],
    "rules": ["MATCH,EGRESS"],
}

target.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
PY

chmod 600 "$CONFIG_DIR/config.yaml" "$CONFIG_DIR/config.download"

if command -v docker >/dev/null 2>&1 && docker service inspect egress_mihomo >/dev/null 2>&1; then
  docker service update --force egress_mihomo >/dev/null
fi

echo "egress config refreshed: $CONFIG_DIR/config.yaml"
