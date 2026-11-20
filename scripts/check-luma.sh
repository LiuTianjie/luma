#!/usr/bin/env bash
# The same source checks run before PR merge, package publishing and image publishing.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

python scripts/bump-version.py --check
python scripts/generate-cli-reference.py --check
python -m ruff check luma tests scripts observe
bash scripts/validate-stacks.sh
# Keep mirrored modules in sync across control, observe, and builder.
cmp -s luma/alert_sources.py observe/alert_sources.py || { echo "DRIFT: luma/alert_sources.py != observe/alert_sources.py" >&2; exit 1; }
cmp -s luma/log_alert_rules.py observe/log_alert_rules.py || { echo "DRIFT: luma/log_alert_rules.py != observe/log_alert_rules.py" >&2; exit 1; }
cmp -s luma/task_results.py observe/task_results.py || { echo "DRIFT: luma/task_results.py != observe/task_results.py" >&2; exit 1; }
# Server and package tests serve these generated assets on a clean checkout.
npm run typecheck:dashboard
npm run build:dashboard
python -m unittest discover -s tests -p 'test_*.py'
# Vendored Phosphor icons must match the generator's mapping.
node scripts/generate-dashboard-icons.mjs --check
node --experimental-strip-types --test tests/dashboard/*.test.mjs
node --test dashboard-src/tests/*.cjs
git diff --check
