#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# ACR settings and registry credentials come from Control.
exec luma build local observe --platform linux/amd64 --workflow-app luma-observe --timeout 7200 "$@"
