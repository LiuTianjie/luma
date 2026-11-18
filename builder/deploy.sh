#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec luma build local builder --compose-sidecar luma.local.compose.yml \
  --platform linux/amd64 --workflow-app luma-build --timeout 7200 "$@"
