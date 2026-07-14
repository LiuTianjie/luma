#!/usr/bin/env bash
# Build the immutable LAE analyzer image on the declared Luma Builder.
set -Eeuo pipefail
umask 077

usage() {
  echo "usage: $0 --repository HTTPS_GIT_URL --commit FULL_SHA --image-repository REGISTRY/REPOSITORY [--builder-prefix NAME] [--insecure-registry]" >&2
  exit 2
}

repository=
commit=
image_repository=
builder_prefix=luma-lae-release
insecure_registry=0
while (($#)); do
  case "$1" in
    --repository) [[ $# -ge 2 ]] || usage; repository=$2; shift 2 ;;
    --commit) [[ $# -ge 2 ]] || usage; commit=$2; shift 2 ;;
    --image-repository) [[ $# -ge 2 ]] || usage; image_repository=$2; shift 2 ;;
    --builder-prefix) [[ $# -ge 2 ]] || usage; builder_prefix=$2; shift 2 ;;
    --insecure-registry) insecure_registry=1; shift ;;
    *) usage ;;
  esac
done

[[ "$repository" =~ ^https://[^[:space:]@]+/[^[:space:]]+\.git$ ]] || {
  echo "repository must be an HTTPS Git URL without embedded credentials" >&2
  exit 2
}
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || {
  echo "commit must be a full 40-character Git object id" >&2
  exit 2
}
[[ "$image_repository" =~ ^[A-Za-z0-9.-]+(:[0-9]{1,5})?/[a-z0-9]+([._-][a-z0-9]+|/[a-z0-9]+([._-][a-z0-9]+)*)*$ ]] || {
  echo "image repository is invalid" >&2
  exit 2
}
[[ "$builder_prefix" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$ ]] || {
  echo "Buildx builder prefix is invalid" >&2
  exit 2
}
command -v git >/dev/null || { echo "git is required" >&2; exit 2; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 2; }
command -v curl >/dev/null || { echo "curl is required" >&2; exit 2; }
# Some sudo policies preserve the invoking user's HOME. Normalize it so Docker
# never reads the caller's credential/config directory.
effective_home=$(getent passwd "$(id -u)" | cut -d: -f6)
[[ -n "$effective_home" && -d "$effective_home" ]] || {
  echo "effective account home is unavailable" >&2
  exit 2
}
export HOME="$effective_home"

work=$(mktemp -d /tmp/luma-lae-agent-runner.XXXXXX)
docker_config="$work/docker-config"
mkdir -m 0700 "$docker_config"
printf '{}\n' >"$docker_config/config.json"
export DOCKER_CONFIG="$docker_config"
builder="${builder_prefix}-${commit:0:12}-$$"
builder_created=0
cleanup() {
  if ((builder_created)); then
    docker buildx rm -f "$builder" >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

# Repository Import also uses a docker-container builder with host networking,
# but its handle lives in a per-task Docker config. A release build creates its
# own short-lived handle so it cannot remove, reuse, or reconfigure an active
# tenant/platform import builder.
docker buildx create \
  --name "$builder" \
  --driver docker-container \
  --driver-opt network=host \
  --bootstrap >/dev/null
builder_created=1
docker buildx inspect "$builder" >/dev/null

git -C "$work" init -q
git -C "$work" remote add origin "$repository"
git -C "$work" fetch -q --depth=1 origin "$commit"
resolved=$(git -C "$work" rev-parse FETCH_HEAD)
[[ "$resolved" = "$commit" ]] || {
  echo "fetched Git object does not match requested commit" >&2
  exit 1
}
git -C "$work" checkout -q --detach "$commit"

short=${commit:0:12}
tag="$image_repository:$short"
metadata="$work/build-metadata.json"
output="type=image,push=true"
registry_scheme=https
if ((insecure_registry)); then
  output+=",registry.insecure=true"
  registry_scheme=http
fi
docker buildx build \
  --builder "$builder" \
  --platform linux/amd64 \
  --pull \
  --provenance=true \
  --sbom=true \
  --file "$work/lae/deploy/luma/docker/agent-runner.Dockerfile" \
  --tag "$tag" \
  --metadata-file "$metadata" \
  --output "$output" \
  "$work/lae"

digest=$(python3 - "$metadata" <<'PY'
import json
import re
import sys

value = json.load(open(sys.argv[1], encoding="utf-8")).get(
    "containerimage.digest", ""
)
if re.fullmatch(r"sha256:[0-9a-f]{64}", str(value)) is None:
    raise SystemExit("Builder did not return an immutable image digest")
print(value)
PY
)
immutable="$image_repository@$digest"
registry_host=${image_repository%%/*}
repository_path=${image_repository#*/}
curl -fsS -o /dev/null \
  -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
  "$registry_scheme://$registry_host/v2/$repository_path/manifests/$digest"
printf '%s\n' "$immutable"
