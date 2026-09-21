#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Keep the release script's pyproject.toml version as the single version source.
# Reading it does not require a Python runtime on Go build or target machines.
version="$(sed -n 's/^version = "\([0-9][0-9.]*\)"$/\1/p' pyproject.toml)"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Unable to read package version from pyproject.toml" >&2
  exit 1
fi
destination="${1:-.local-notes/go-bin/luma-go}"
entry="${2:-luma}"
case "$entry" in
  luma|luma-agent|luma-control) ;;
  *) echo "Unsupported Go executable: $entry" >&2; exit 1 ;;
esac
mkdir -p "$(dirname "$destination")"
CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=$version" -o "$destination" "./cmd/$entry"
echo "Built $destination (version $version)"
