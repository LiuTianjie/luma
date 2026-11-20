#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
SIDECAR="${LUMA_EYE_COMPOSE_SIDECAR:-deploy/node-edge/luma.local.compose.yml}"
if [[ ! -f "${SIDECAR}" ]]; then
  echo "Missing ${SIDECAR}. Copy deploy/node-edge/luma.compose.yml -> deploy/node-edge/luma.local.compose.yml (and docker-compose.yml -> docker-compose.local.yml) and fill in your node/domain." >&2
  exit 1
fi
# Registry host/namespace and credentials come from the active Luma Control context.
exec luma build local . --workflow-app luma-eye --compose-sidecar "${SIDECAR}" --platform linux/amd64 --repo-url local/luma-eye --timeout 7200 "$@"
