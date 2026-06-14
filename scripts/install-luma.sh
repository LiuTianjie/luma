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
LUMA_USER_HOME="${LUMA_USER_HOME:-${HOME:-}}"
if [ -z "$LUMA_USER_HOME" ]; then
  if command -v getent >/dev/null 2>&1; then
    LUMA_USER_HOME="$(getent passwd "$(id -u)" | awk -F: '{print $6}' || true)"
  fi
  if [ -z "$LUMA_USER_HOME" ] && command -v dscl >/dev/null 2>&1; then
    LUMA_USER_NAME="$(id -un 2>/dev/null || true)"
    if [ -n "$LUMA_USER_NAME" ]; then
      LUMA_USER_HOME="$(dscl . -read "/Users/$LUMA_USER_NAME" NFSHomeDirectory 2>/dev/null | awk '{print $2}' || true)"
    fi
  fi
fi
if [ -z "$LUMA_USER_HOME" ]; then
  echo "HOME is not set and the current user's home directory could not be resolved." >&2
  exit 1
fi
HOME="$LUMA_USER_HOME"
export HOME
INSTALL_HOME="${LUMA_INSTALL_HOME:-$LUMA_USER_HOME/.local/share/luma}"
BIN_DIR="${LUMA_BIN_DIR:-$LUMA_USER_HOME/.local/bin}"
OWNER_SPEC=""

resolve_install_owner() {
  [ "$(id -u)" -eq 0 ] || return 0
  if [ -n "${LUMA_INSTALL_OWNER:-}" ]; then
    OWNER_SPEC="$LUMA_INSTALL_OWNER"
  elif [ -d "$LUMA_USER_HOME" ]; then
    if OWNER_SPEC="$(stat -c '%u:%g' "$LUMA_USER_HOME" 2>/dev/null)"; then
      :
    elif OWNER_SPEC="$(stat -f '%u:%g' "$LUMA_USER_HOME" 2>/dev/null)"; then
      :
    else
      OWNER_SPEC=""
    fi
  fi
  case "$OWNER_SPEC" in
    ""|0:0) OWNER_SPEC="" ;;
  esac
}

chown_install_paths() {
  [ -n "$OWNER_SPEC" ] || return 0
  for path in "$LUMA_USER_HOME/.local" "$LUMA_USER_HOME/.local/share" "$BIN_DIR"; do
    [ -e "$path" ] || continue
    chown "$OWNER_SPEC" "$path" 2>/dev/null || true
  done
  [ -e "$INSTALL_HOME" ] && chown -R "$OWNER_SPEC" "$INSTALL_HOME" 2>/dev/null || true
  [ -e "$BIN_DIR/luma" ] && chown "$OWNER_SPEC" "$BIN_DIR/luma" 2>/dev/null || true
  for profile in "$HOME/.profile" "$HOME/.bash_profile" "$HOME/.bashrc" "$HOME/.zprofile" "$HOME/.zshrc"; do
    [ -e "$profile" ] || continue
    chown "$OWNER_SPEC" "$profile" 2>/dev/null || true
  done
}

resolve_install_owner

ensure_path() {
  case ":$PATH:" in
    *":$BIN_DIR:"*) return 0 ;;
  esac

  marker="# Luma CLI"
  line="export PATH=\"$BIN_DIR:\$PATH\""
  updated=""

  for profile in "$HOME/.profile" "$HOME/.bash_profile" "$HOME/.bashrc" "$HOME/.zprofile" "$HOME/.zshrc"; do
    [ -f "$profile" ] || continue
    if ! grep -F "$BIN_DIR" "$profile" >/dev/null 2>&1; then
      {
        printf '\n%s\n' "$marker"
        printf '%s\n' "$line"
      } >> "$profile"
      updated="${updated}${updated:+ }$profile"
    fi
  done

  if [ -z "$updated" ]; then
    profile="$HOME/.profile"
    {
      printf '\n%s\n' "$marker"
      printf '%s\n' "$line"
    } >> "$profile"
    updated="$profile"
  fi

  PATH="$BIN_DIR:$PATH"
  export PATH
  echo "PATH updated in: $updated"
}

download_source() {
  case "$INSTALL_REF" in
    refs/*)
      default_archive_url="$REPO_URL/archive/$INSTALL_REF.tar.gz"
      ;;
    v[0-9]*|[0-9]*.[0-9]*)
      default_archive_url="$REPO_URL/archive/refs/tags/$INSTALL_REF.tar.gz"
      ;;
    *)
      default_archive_url="$REPO_URL/archive/refs/heads/$INSTALL_REF.tar.gz"
      ;;
  esac
  archive_url="${LUMA_ARCHIVE_URL:-$default_archive_url}"
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

refresh_node_agent_service() {
  agent_config="/opt/luma/node-agent/agent.json"
  [ -f "$agent_config" ] || return 0
  [ -x "$BIN_DIR/luma" ] || return 0

  os_name="$(uname -s 2>/dev/null || echo unknown)"
  case "$os_name" in
    Darwin)
      plist="/Library/LaunchDaemons/io.luma.node-agent.plist"
      tmp_plist="$(mktemp)"
      cat > "$tmp_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.luma.node-agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN_DIR/luma</string>
    <string>node-agent</string>
    <string>run</string>
    <string>--config</string>
    <string>$agent_config</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/var/log/luma-node-agent.log</string>
  <key>StandardErrorPath</key>
  <string>/var/log/luma-node-agent.err</string>
</dict>
</plist>
EOF
      run_sudo install -m 0644 "$tmp_plist" "$plist"
      rm -f "$tmp_plist"
      run_sudo sh -c "( sleep ${LUMA_AGENT_RELOAD_DELAY_SECONDS:-20}; launchctl bootout system/io.luma.node-agent >/dev/null 2>&1 || true; launchctl bootstrap system $plist; launchctl kickstart -k system/io.luma.node-agent ) >/tmp/luma-node-agent-reload.log 2>&1 &"
      echo "Luma node agent launchd reload scheduled"
      ;;
    Linux)
      if command -v systemctl >/dev/null 2>&1; then
        tmp_unit="$(mktemp)"
        cat > "$tmp_unit" <<EOF
[Unit]
Description=Luma node agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=$BIN_DIR/luma node-agent run --config $agent_config
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
        run_sudo install -m 0644 "$tmp_unit" /etc/systemd/system/luma-node-agent.service
        rm -f "$tmp_unit"
        run_sudo systemctl daemon-reload
        run_sudo systemctl enable luma-node-agent.service >/dev/null
        run_sudo sh -c "( sleep ${LUMA_AGENT_RELOAD_DELAY_SECONDS:-20}; systemctl restart luma-node-agent.service ) >/tmp/luma-node-agent-reload.log 2>&1 &"
        echo "Luma node agent systemd restart scheduled"
      fi
      ;;
  esac
}

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
python -m pip install --upgrade pip || echo "[warn] pip upgrade failed; continuing with existing pip"
python -m pip install --upgrade "setuptools>=77" wheel || echo "[warn] build backend install failed; continuing with existing build backend"

pip_install_luma() {
  set +e
  if [ "${LUMA_PIP_BUILD_ISOLATION:-0}" = "1" ]; then
    pip install "$@"
  else
    pip install --no-build-isolation "$@"
  fi
  code=$?
  set -e
  return "$code"
}

INSTALL_SUCCEEDED=0
if [ -n "$INSTALL_MODE" ]; then
  if pip_install_luma "$INSTALL_MODE" "$SOURCE_DIR"; then
    INSTALL_SUCCEEDED=1
  fi
else
  if pip_install_luma "$SOURCE_DIR"; then
    INSTALL_SUCCEEDED=1
  fi
fi
if [ "$INSTALL_SUCCEEDED" -eq 0 ]; then
  echo "[warn] package install failed; using source checkout with existing venv dependencies"
fi

if [ "$LOCAL_CHECKOUT" -eq 0 ]; then
  mkdir -p "$BIN_DIR"
  cat > "$BIN_DIR/luma" <<EOF
#!/usr/bin/env sh
PYTHONPATH="$SOURCE_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
export PYTHONPATH
if [ -x "$VENV_DIR/bin/luma" ]; then
  exec "$VENV_DIR/bin/luma" "\$@"
fi
exec "$VENV_DIR/bin/python" -m luma.cli "\$@"
EOF
  chmod +x "$BIN_DIR/luma"
  ensure_path
  refresh_node_agent_service
  chown_install_paths
fi

echo "Luma installed in $VENV_DIR"
if [ "$LOCAL_CHECKOUT" -eq 0 ]; then
  echo "Command shim: $BIN_DIR/luma"
  echo "Open a new shell or run: exec \$SHELL -l"
fi
echo "Next:"
if [ "$LOCAL_CHECKOUT" -eq 1 ]; then
  echo "  . $VENV_DIR/bin/activate"
  echo "  luma preflight"
  echo "If your shell resolves ./luma instead, run:"
  echo "  $VENV_DIR/bin/luma preflight"
  echo "  ./scripts/luma preflight"
else
  echo "  $BIN_DIR/luma preflight"
  echo "  $BIN_DIR/luma login https://luma.example.com --token <deploy-token>"
fi
