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

run_sudo() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif [ -n "${LUMA_SUDO_PASSWORD:-}" ]; then
    printf '%s\n' "$LUMA_SUDO_PASSWORD" | sudo -S "$@"
  else
    sudo "$@"
  fi
}

can_run_sudo_noninteractive() {
  if [ "$(id -u)" -eq 0 ] || [ -n "${LUMA_SUDO_PASSWORD:-}" ]; then
    return 0
  fi
  sudo -n true >/dev/null 2>&1
}

repair_install_ownership() {
  [ "$(id -u)" -ne 0 ] || return 0
  [ -e "$INSTALL_HOME" ] || return 0

  needs_repair=0
  [ -w "$INSTALL_HOME" ] || needs_repair=1
  if [ -e "$INSTALL_HOME/src" ]; then
    [ -w "$INSTALL_HOME/src" ] || needs_repair=1
    if [ -n "$(find "$INSTALL_HOME/src" ! -user "$(id -u)" -print -quit 2>/dev/null)" ]; then
      needs_repair=1
    fi
  fi
  [ "$needs_repair" -eq 1 ] || return 0

  if ! can_run_sudo_noninteractive; then
    echo "Install directory contains files not owned by $(id -un); rerun with sudo or set LUMA_SUDO_PASSWORD." >&2
    exit 1
  fi
  run_sudo chown -R "$(id -u):$(id -g)" "$INSTALL_HOME"
}

is_commit_ref() {
  [ "${#1}" -eq 40 ] || return 1
  case "$1" in
    *[!0-9a-fA-F]*) return 1 ;;
  esac
  return 0
}

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
      if is_commit_ref "$INSTALL_REF"; then
        default_archive_url="$REPO_URL/archive/$INSTALL_REF.tar.gz"
      else
        default_archive_url="$REPO_URL/archive/refs/heads/$INSTALL_REF.tar.gz"
      fi
      ;;
  esac
  archive_url="${LUMA_ARCHIVE_URL:-$default_archive_url}"
  tmp_dir="$(mktemp -d)"
  archive="$tmp_dir/luma.tar.gz"
  download_connect_timeout="${LUMA_DOWNLOAD_CONNECT_TIMEOUT_SECONDS:-20}"
  download_max_time="${LUMA_DOWNLOAD_MAX_TIME_SECONDS:-300}"
  download_retries="${LUMA_DOWNLOAD_RETRIES:-4}"
  case "$download_connect_timeout" in *[!0-9]*|"") download_connect_timeout=20 ;; esac
  case "$download_max_time" in *[!0-9]*|"") download_max_time=300 ;; esac
  case "$download_retries" in *[!0-9]*|"") download_retries=4 ;; esac
  echo "Downloading Luma source for $INSTALL_REF (timeout ${download_max_time}s, retries ${download_retries})"
  if command -v curl >/dev/null 2>&1; then
    if curl --help all 2>/dev/null | grep -q -- '--retry-all-errors'; then
      curl -fsSL --connect-timeout "$download_connect_timeout" --max-time "$download_max_time" \
        --retry "$download_retries" --retry-delay 2 --retry-all-errors "$archive_url" -o "$archive"
    else
      curl -fsSL --connect-timeout "$download_connect_timeout" --max-time "$download_max_time" \
        --retry "$download_retries" --retry-delay 2 "$archive_url" -o "$archive"
    fi
  elif command -v wget >/dev/null 2>&1; then
    wget -q --tries="$download_retries" --timeout="$download_connect_timeout" -O "$archive" "$archive_url"
  else
    echo "curl or wget is required to download Luma." >&2
    exit 1
  fi
  mkdir -p "$INSTALL_HOME"
  repair_install_ownership
  tar -xzf "$archive" -C "$tmp_dir"
  extracted="$(find "$tmp_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [ -z "$extracted" ]; then
    echo "Downloaded archive did not contain a source directory." >&2
    exit 1
  fi
  # Never replace source imported by the running agent. Create the candidate at
  # its final path (Python venv console scripts contain absolute shebangs).
  mkdir -p "$INSTALL_HOME/releases"
  CANDIDATE_DIR="$(mktemp -d "$INSTALL_HOME/releases/candidate.XXXXXXXX")"
  cp -R "$extracted" "$CANDIDATE_DIR/src"
  rm -rf "$tmp_dir"
  SOURCE_DIR="$CANDIDATE_DIR/src"
}

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.9+ and venv support are required before installing Luma." >&2
  exit 1
fi

if [ "$LOCAL_CHECKOUT" -eq 0 ]; then
  # Serialize standalone CLI and agent-driven installers on the installation,
  # not on a user HOME. flock releases automatically even on process failure.
  mkdir -p "$INSTALL_HOME"
  repair_install_ownership
  # Keep the open file description in this shell. Python only acquires flock
  # on the inherited descriptor: no re-exec, so curl | sh works as well.
  # Append mode does not truncate a pre-existing lock target before validation.
  exec 9>> "$INSTALL_HOME/.installer.lock"
  python3 - "$INSTALL_HOME/.installer.lock" <<'PYLOCK'
import fcntl, os, stat, sys
path_info = os.lstat(sys.argv[1])
fd_info = os.fstat(9)
if (not stat.S_ISREG(path_info.st_mode)
        or (path_info.st_dev, path_info.st_ino) != (fd_info.st_dev, fd_info.st_ino)):
    sys.exit("Luma installer lock must be a regular, non-symlink file")
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit("Another Luma installation is in progress; wait for that operation.")
PYLOCK
  if [ -n "${LUMA_VENV_DIR:-}" ]; then
    echo "LUMA_VENV_DIR is only supported for development checkouts; managed installs use isolated candidates." >&2
    exit 1
  fi
  download_source
fi

cd "$SOURCE_DIR"

if [ "$LOCAL_CHECKOUT" -eq 1 ] && [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

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
After=network-online.target docker.service nomad.service
Wants=network-online.target docker.service nomad.service
StartLimitIntervalSec=0

[Service]
Type=simple
EnvironmentFile=-/etc/default/luma-node-agent
ExecStart=$BIN_DIR/luma node-agent run --config $agent_config
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
        if run_sudo test -f /etc/systemd/system/luma-node-agent.service; then
          run_sudo cp -a /etc/systemd/system/luma-node-agent.service "/etc/systemd/system/luma-node-agent.service.luma-backup-$(date +%Y%m%d%H%M%S)"
        fi
        run_sudo install -m 0644 "$tmp_unit" /etc/systemd/system/luma-node-agent.service
        rm -f "$tmp_unit"
        run_sudo systemctl daemon-reload
        run_sudo systemctl enable luma-node-agent.service >/dev/null
        run_sudo systemctl reset-failed luma-node-agent.service >/dev/null 2>&1 || true
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
  VENV_DIR="$CANDIDATE_DIR/venv"
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
# A caller's Python path must not supply missing candidate dependencies.
unset PYTHONHOME PYTHONPATH
export PYTHONNOUSERSITE=1
. "$VENV_DIR/bin/activate"
export SOURCE_DIR INSTALL_HOME BIN_DIR LUMA_USER_HOME
# One dependency policy for bootstrap, CLI and Dashboard. Do not inherit host
# pip.conf/PIP_EXTRA_INDEX_URL/trusted-host settings in the managed runtime.
pip() {
  "$VENV_DIR/bin/python" "$SOURCE_DIR/luma/installation.py" pip "$@"
}
pip install --upgrade pip || echo "[warn] pip upgrade failed; continuing with existing pip"
if ! pip install --upgrade "setuptools>=77" wheel; then
  if [ "$LOCAL_CHECKOUT" -eq 0 ]; then
    echo "Luma dependency preparation failed (build backend); old runtime and entry unchanged. Set LUMA_PIP_INDEX_URL or LUMA_PIP_WHEELHOUSE to a reachable approved source." >&2
    exit 1
  fi
  echo "[warn] build backend install failed; continuing with existing build backend"
fi

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

prune_stale_luma_metadata() {
  source_version="$(sed -n 's/^__version__ = "\([^"]*\)"/\1/p' "$SOURCE_DIR/luma/__init__.py" | head -n 1)"
  [ -n "$source_version" ] || return 0
  expected="luma_infra-${source_version}.dist-info"
  for metadata in "$VENV_DIR"/lib/python*/site-packages/luma_infra-*.dist-info; do
    [ -d "$metadata" ] || continue
    [ "$(basename "$metadata")" = "$expected" ] || rm -rf "$metadata"
  done
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
  if [ "$LOCAL_CHECKOUT" -eq 0 ]; then
    echo "Luma dependency preparation failed (package); old runtime and entry unchanged. Inspect the configured dependency source and candidate log." >&2
    exit 1
  fi
  echo "[warn] package install failed; using source checkout with existing venv dependencies"
else
  prune_stale_luma_metadata
fi

# A failed pip install may only fall back to source when the target runtime
# actually works. Never repoint a healthy agent to a newly created empty venv.
validate_luma_runtime() {
  PYTHONPATH="$SOURCE_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV_DIR/bin/python" -c '
import yaml, starlette, uvicorn, websockets, python_socks
import luma.cli
' || return 1
  PYTHONPATH="$SOURCE_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV_DIR/bin/python" -m luma.cli node-agent run --help >/dev/null || return 1
  "$VENV_DIR/bin/python" -m pip check || return 1
}
if ! validate_luma_runtime; then
  echo "Luma runtime validation failed; leaving command shim and node agent service unchanged." >&2
  exit 1
fi

if [ "$LOCAL_CHECKOUT" -eq 0 ]; then
  # Persist the final intended owner, including root preparing an operator's runtime.
  if [ -n "$OWNER_SPEC" ]; then
    chown -R "$OWNER_SPEC" "$CANDIDATE_DIR"
  fi
  "$VENV_DIR/bin/python" "$SOURCE_DIR/luma/installation.py" record
  mkdir -p "$BIN_DIR"
  shim_tmp="$(mktemp "$BIN_DIR/.luma.XXXXXXXX")"
  "$VENV_DIR/bin/python" "$SOURCE_DIR/luma/installation.py" shim > "$shim_tmp"
  chmod 755 "$shim_tmp"
  mv -f "$shim_tmp" "$BIN_DIR/luma"
  ensure_path
  if [ "${LUMA_SKIP_NODE_AGENT_SERVICE_REFRESH:-0}" = "1" ]; then
    echo "Luma node agent service refresh deferred"
  else
    refresh_node_agent_service
  fi
  chown_install_paths
fi

echo "Luma installed in $VENV_DIR"
installed_version="$(sed -n 's/^__version__ = "\([^"]*\)"/\1/p' "$SOURCE_DIR/luma/__init__.py" | head -n 1)"
[ -n "$installed_version" ] || {
  echo "Installed Luma source does not declare a version." >&2
  exit 1
}
echo "Luma version: $installed_version"
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
