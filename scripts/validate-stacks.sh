#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "error: Docker is not installed or not available in PATH." >&2
  echo "Install Docker first, then rerun this script." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "error: Docker Compose plugin is not available." >&2
  echo "Install the Docker Compose plugin so 'docker compose' works." >&2
  exit 1
fi

found=0

while IFS= read -r -d '' file; do
  found=1
  echo "validating ${file}"
  docker compose -f "${file}" config >/dev/null
done < <(find stacks -path '*/stack.yml' -type f -print0 | sort -z)

if [ "${found}" -eq 0 ]; then
  echo "error: no stack.yml files found under stacks/." >&2
  exit 1
fi

echo "all stack files are valid"
