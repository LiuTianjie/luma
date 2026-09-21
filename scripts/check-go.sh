#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v go >/dev/null 2>&1; then
  echo "Go is required; install the version declared in go.mod." >&2
  exit 1
fi

unformatted="$(gofmt -l cmd internal tests/goarchitecture)"
if [[ -n "$unformatted" ]]; then
  echo "Go files need gofmt:" >&2
  echo "$unformatted" >&2
  exit 1
fi

# The LAE workspace is independently owned and is not part of the Luma rewrite.
go vet ./cmd/... ./internal/... ./tests/goarchitecture/...
go test -race ./cmd/... ./internal/... ./tests/goarchitecture/...
