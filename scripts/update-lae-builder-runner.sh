#!/usr/bin/env bash
# Atomically rotate the immutable LAE analyzer image on an initialized Builder.
set -Eeuo pipefail
umask 077

[[ ${EUID} -eq 0 ]] || { echo "run as root" >&2; exit 2; }
[[ $# -eq 1 ]] || { echo "usage: $0 REPOSITORY@sha256:DIGEST" >&2; exit 2; }
image=$1
[[ "$image" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || {
  echo "runner image must be an immutable repository digest" >&2; exit 2;
}

builder_user=ubuntu
builder_uid=$(id -u "$builder_user")
[[ "$builder_uid" = 1000 ]] || { echo "Builder user must be ubuntu uid 1000" >&2; exit 2; }
runtime_dir="/run/user/$builder_uid"
docker_host="unix://$runtime_dir/docker.sock"
env_file=/etc/default/luma-node-agent
[[ -S "$runtime_dir/docker.sock" ]] || { echo "rootless Docker socket is unavailable" >&2; exit 2; }
[[ -f "$env_file" && ! -L "$env_file" ]] || { echo "node agent env is invalid" >&2; exit 2; }

as_builder() {
  runuser -u "$builder_user" -- env HOME="$(getent passwd "$builder_user" | cut -d: -f6)" \
    XDG_RUNTIME_DIR="$runtime_dir" DOCKER_HOST="$docker_host" "$@"
}

as_builder docker --host "$docker_host" pull "$image"
repo_digests=$(as_builder docker --host "$docker_host" image inspect \
  --format '{{json .RepoDigests}}' "$image")
grep -Fq '"'"$image"'"' <<<"$repo_digests" || {
  echo "pulled image does not expose the required RepoDigest" >&2; exit 2;
}

temporary=$(mktemp "${env_file}.XXXXXX")
trap 'rm -f "$temporary"' EXIT
python3 - "$env_file" "$temporary" "$image" <<'PY'
import os, sys
from pathlib import Path
source = Path(sys.argv[1])
target = Path(sys.argv[2])
image = sys.argv[3]
lines = source.read_text(encoding="utf-8").splitlines()
matches = 0
result = []
for line in lines:
    if line.startswith("LUMA_BUILDER_ANALYZE_IMAGE_DIGEST="):
        result.append("LUMA_BUILDER_ANALYZE_IMAGE_DIGEST=" + image)
        matches += 1
    else:
        result.append(line)
if matches != 1:
    raise SystemExit("node agent env must contain exactly one analyzer digest")
target.write_text("\n".join(result) + "\n", encoding="utf-8")
os.chmod(target, 0o600)
PY
install -m 0644 -o root -g root "$temporary" "$env_file"
systemctl restart luma-node-agent.service
systemctl is-active --quiet luma-node-agent.service
echo "LAE Builder analyzer digest updated"
