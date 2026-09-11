#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "[1/4] prerequisite and first-install scenario contract"
python -m unittest \
  tests.test_first_install_scenarios \
  tests.test_dependencies \
  tests.test_dashboard_setup \
  tests.test_deployment_health

echo "[2/4] render every shipped manifest"
bash scripts/validate-stacks.sh

echo "[3/4] dashboard typecheck"
npm run typecheck:dashboard

echo "[4/4] dashboard production build"
npm run build:dashboard

echo "First-install validation passed."
