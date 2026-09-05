#!/usr/bin/env bash
# The same source checks run before PR merge, package publishing and image publishing.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

python scripts/bump-version.py --check
python scripts/generate-cli-reference.py --check
python -m unittest discover -s tests -p 'test_*.py'
node --experimental-strip-types --test tests/dashboard/*.test.mjs
node --test dashboard-src/tests/*.cjs
npm run typecheck:dashboard
npm run build:dashboard
git diff --check
