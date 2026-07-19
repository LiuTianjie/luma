#!/bin/sh
set -eu

# Luma Dashboard uses a file-only admin credential. Compose deployments
# currently receive scoped secrets as environment variables, so convert that
# one value into a private process-local file before starting the API and
# remove the plaintext variable from the child environment.
if [ -n "${LAE_ADMIN_API_TOKEN:-}" ]; then
  if [ -n "${LAE_ADMIN_API_TOKEN_FILE:-}" ]; then
    echo "LAE admin token source is ambiguous" >&2
    exit 1
  fi
  umask 077
  LAE_ADMIN_API_TOKEN_FILE=/tmp/lae-admin-api.token
  printf '%s\n' "$LAE_ADMIN_API_TOKEN" >"$LAE_ADMIN_API_TOKEN_FILE"
  unset LAE_ADMIN_API_TOKEN
  export LAE_ADMIN_API_TOKEN_FILE
fi

if [ "${LAE_RUN_MIGRATIONS:-0}" = "1" ]; then
  migration_attempt=0
  until alembic -c /opt/lae/migrations/alembic.ini upgrade head; do
    migration_attempt=$((migration_attempt + 1))
    if [ "$migration_attempt" -ge "${LAE_MIGRATION_MAX_ATTEMPTS:-60}" ]; then
      echo "database migration did not become ready" >&2
      exit 1
    fi
    sleep "${LAE_MIGRATION_RETRY_SECONDS:-2}"
  done
fi

exec lae-api --serve --host 0.0.0.0 --port 8080
