#!/usr/bin/env sh
set -eu

INSTALL_HOME="${LUMA_INSTALL_HOME:-$HOME/.local/share/luma}"
BIN_DIR="${LUMA_BIN_DIR:-$HOME/.local/bin}"
CONFIG_FILE="${LUMA_USER_CONFIG:-$HOME/.luma.config.json}"
CONTEXT_HOME="${LUMA_CONFIG_HOME:-$HOME/.config/luma}"
PURGE=0

usage() {
  cat <<'EOF'
Usage: uninstall-luma.sh [--purge]

Removes the local Luma CLI install:
  - ~/.local/bin/luma
  - ~/.local/share/luma

By default this keeps user secrets and login contexts.
Use --purge to also remove:
  - ~/.luma.config.json
  - ~/.config/luma

This does not remove Docker, Nomad, Luma Control state, or deployed services.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --purge)
      PURGE=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

removed=0

if [ -e "$BIN_DIR/luma" ]; then
  rm -f "$BIN_DIR/luma"
  echo "Removed: $BIN_DIR/luma"
  removed=1
fi

if [ -e "$INSTALL_HOME" ]; then
  rm -rf "$INSTALL_HOME"
  echo "Removed: $INSTALL_HOME"
  removed=1
fi

if [ "$PURGE" -eq 1 ]; then
  if [ -e "$CONFIG_FILE" ]; then
    rm -f "$CONFIG_FILE"
    echo "Removed: $CONFIG_FILE"
    removed=1
  fi
  if [ -e "$CONTEXT_HOME" ]; then
    rm -rf "$CONTEXT_HOME"
    echo "Removed: $CONTEXT_HOME"
    removed=1
  fi
else
  echo "Kept user config: $CONFIG_FILE"
  echo "Kept login contexts: $CONTEXT_HOME"
fi

if [ "$removed" -eq 0 ]; then
  echo "No local Luma CLI install found."
fi

echo "Done."
