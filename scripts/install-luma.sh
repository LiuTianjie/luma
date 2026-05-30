#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
LOCAL_CHECKOUT=0
if [ -f "$ROOT/pyproject.toml" ] && [ -d "$ROOT/luma" ]; then
  LOCAL_CHECKOUT=1
  SOURCE_DIR="$ROOT"
else
  SOURCE_DIR=""
fi

REPO_URL="${LUMA_REPO_URL:-https://github.com/LiuTianjie/luma}"
INSTALL_REF="${LUMA_INSTALL_REF:-main}"
INSTALL_HOME="${LUMA_INSTALL_HOME:-$HOME/.local/share/luma}"
BIN_DIR="${LUMA_BIN_DIR:-$HOME/.local/bin}"

download_source() {
  archive_url="${LUMA_ARCHIVE_URL:-$REPO_URL/archive/refs/heads/$INSTALL_REF.tar.gz}"
  tmp_dir="$(mktemp -d)"
  archive="$tmp_dir/luma.tar.gz"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$archive_url" -o "$archive"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$archive" "$archive_url"
  else
    echo "curl or wget is required to download Luma." >&2
    exit 1
  fi
  mkdir -p "$INSTALL_HOME"
  tar -xzf "$archive" -C "$tmp_dir"
  extracted="$(find "$tmp_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [ -z "$extracted" ]; then
    echo "Downloaded archive did not contain a source directory." >&2
    exit 1
  fi
  rm -rf "$INSTALL_HOME/src"
  cp -R "$extracted" "$INSTALL_HOME/src"
  rm -rf "$tmp_dir"
  SOURCE_DIR="$INSTALL_HOME/src"
}

if [ "$LOCAL_CHECKOUT" -eq 0 ]; then
  download_source
fi

cd "$SOURCE_DIR"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

run_sudo() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif [ -n "${LUMA_SUDO_PASSWORD:-}" ]; then
    printf '%s\n' "$LUMA_SUDO_PASSWORD" | sudo -S "$@"
  else
    sudo "$@"
  fi
}

configure_dns() {
  if [ "$(uname -s)" != "Linux" ] || ! command -v resolvectl >/dev/null 2>&1; then
    return 0
  fi
  if ! command -v systemctl >/dev/null 2>&1 || ! systemctl list-unit-files systemd-resolved.service >/dev/null 2>&1; then
    return 0
  fi
  run_sudo install -d -m 755 /etc/systemd/resolved.conf.d
  tmp="$(mktemp)"
  cat > "$tmp" <<'EOF'
[Resolve]
DNS=223.5.5.5 119.29.29.29 1.1.1.1
FallbackDNS=8.8.8.8 9.9.9.9
Domains=~.
EOF
  run_sudo install -m 644 "$tmp" /etc/systemd/resolved.conf.d/luma.conf
  rm -f "$tmp"
  run_sudo systemctl restart systemd-resolved || true
  iface="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
  if [ -n "$iface" ]; then
    run_sudo resolvectl dns "$iface" 223.5.5.5 119.29.29.29 1.1.1.1 || true
    run_sudo resolvectl domain "$iface" '~.' || true
  fi
}

configure_dns

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required."
  echo "macOS: brew install python"
  echo "Ubuntu/Debian: sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip"
  exit 1
fi

PY_VERSION="$(python3 - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
)" || {
  echo "Python 3.9+ is required. Current python3 is $PY_VERSION."
  exit 1
}

if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "python3 venv support is missing."
  if command -v apt-get >/dev/null 2>&1; then
    run_sudo apt-get update
    run_sudo apt-get install -y python3-venv python3-pip
  else
    echo "Ubuntu/Debian: sudo apt-get install -y python3-venv python3-pip"
    exit 1
  fi
fi

if [ "$LOCAL_CHECKOUT" -eq 1 ]; then
  VENV_DIR="${LUMA_VENV_DIR:-$SOURCE_DIR/.venv}"
  INSTALL_MODE="-e"
else
  VENV_DIR="${LUMA_VENV_DIR:-$INSTALL_HOME/venv}"
  INSTALL_MODE=""
fi

if ! python3 -m venv "$VENV_DIR"; then
  if command -v apt-get >/dev/null 2>&1; then
    run_sudo apt-get update
    run_sudo apt-get install -y python3-venv python3-pip
    rm -rf "$VENV_DIR"
    python3 -m venv "$VENV_DIR"
  else
    exit 1
  fi
fi
. "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
if [ -n "$INSTALL_MODE" ]; then
  pip install "$INSTALL_MODE" "$SOURCE_DIR"
else
  pip install "$SOURCE_DIR"
fi

if [ "$LOCAL_CHECKOUT" -eq 0 ]; then
  mkdir -p "$BIN_DIR"
  cat > "$BIN_DIR/luma" <<EOF
#!/usr/bin/env sh
exec "$VENV_DIR/bin/luma" "\$@"
EOF
  chmod +x "$BIN_DIR/luma"
fi

echo "Luma installed in $VENV_DIR"
if [ "$LOCAL_CHECKOUT" -eq 0 ]; then
  echo "Command shim: $BIN_DIR/luma"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "Add $BIN_DIR to PATH if 'luma' is not found." ;;
  esac
fi
echo "Next:"
if [ "$LOCAL_CHECKOUT" -eq 1 ]; then
  echo "  . $VENV_DIR/bin/activate"
  echo "  luma preflight"
  echo "If your shell resolves ./luma instead, run:"
  echo "  $VENV_DIR/bin/luma preflight"
  echo "  ./scripts/luma preflight"
else
  echo "  luma preflight"
  echo "  luma login https://luma.example.com --token <deploy-token>"
fi
