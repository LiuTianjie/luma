#!/usr/bin/env bash
set -euo pipefail

found=0

while IFS= read -r -d '' file; do
  found=1
  echo "validating ${file}"
  python -m luma.cli validate "${file}" >/dev/null
  python -m luma.cli render "${file}" >/dev/null
done < <(find examples templates \( -name '*.yaml' -o -name '*.yml' \) -type f -print0 | sort -z)

if [ "${found}" -eq 0 ]; then
  echo "error: no Luma manifest templates found under examples/ or templates/." >&2
  exit 1
fi

echo "all Luma manifests render as Nomad jobs"
