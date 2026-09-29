#!/usr/bin/env bash
set -euo pipefail

found=0

while IFS= read -r -d '' file; do
  found=1
  echo "validating ${file}"
  python -m luma.cli --no-env validate "${file}" >/dev/null
  python -m luma.cli --no-env deploy "${file}" --dry-run >/dev/null
done < <(find templates \( -name '*.yaml' -o -name '*.yml' \) -type f -print0 | sort -z)

if [ "${found}" -eq 0 ]; then
  echo "error: no Luma manifest templates found under templates/." >&2
  exit 1
fi

echo "all Luma manifests render as Nomad jobs"
