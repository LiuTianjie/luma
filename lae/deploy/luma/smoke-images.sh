#!/bin/sh
set -eu

WEB_IMAGE=${LAE_WEB_IMAGE:-lae-web:asset-test}
API_IMAGE=${LAE_API_IMAGE:-lae-api:asset-test}
WORKER_IMAGE=${LAE_WORKER_IMAGE:-lae-worker:asset-test}
CONTROLLER_IMAGE=${LAE_CONTROLLER_IMAGE:-lae-agent-controller:asset-test}
RUNNER_IMAGE=${LAE_RUNNER_IMAGE:-lae-agent-runner:asset-test}

require_non_root() {
  image=$1
  user=$(docker image inspect "$image" --format '{{.Config.User}}')
  case "$user" in
    ""|0|0:0|root|root:root)
      echo "$image does not declare a non-root runtime user" >&2
      exit 1
      ;;
  esac
}

check_python_entrypoint() {
  image=$1
  command=$2
  require_non_root "$image"
  docker run --rm --platform linux/amd64 --entrypoint python "$image" -c \
    "from pathlib import Path; p=Path('/opt/lae/.venv/bin/$command'); assert p.is_file(); assert p.read_text().splitlines()[0] == '#!/opt/lae/.venv/bin/python'"
  docker run --rm --platform linux/amd64 --entrypoint "$command" "$image" --health >/dev/null
}

require_non_root "$WEB_IMAGE"
docker run --rm --platform linux/amd64 --entrypoint node "$WEB_IMAGE" -e \
  "const fs=require('node:fs');if(!fs.existsSync('/app/apps/web/server.js'))process.exit(1);const m=require('/app/apps/web/.next/routes-manifest.json');const r=m.rewrites.afterFiles.find(x=>x.source==='/v1/:path*');if(!r||r.destination!=='http://api:8080/v1/:path*')process.exit(1)"

check_python_entrypoint "$API_IMAGE" lae-api
docker run --rm --platform linux/amd64 --entrypoint alembic "$API_IMAGE" --version >/dev/null
check_python_entrypoint "$WORKER_IMAGE" lae-worker
check_python_entrypoint "$CONTROLLER_IMAGE" lae-agent-controller
check_python_entrypoint "$RUNNER_IMAGE" lae-agent-runner

echo "LAE image smoke checks passed"
