#!/usr/bin/env bash
# Build the immutable LAE analyzer image on the declared Luma Builder.
set -Eeuo pipefail
umask 077

usage() {
  echo "usage: $0 --repository HTTPS_GIT_URL --commit FULL_SHA --image-repository REGISTRY/REPOSITORY [--builder NAME]" >&2
  exit 2
}

repository=
commit=
image_repository=
builder=luma-builder
while (($#)); do
  case "$1" in
    --repository) [[ $# -ge 2 ]] || usage; repository=$2; shift 2 ;;
    --commit) [[ $# -ge 2 ]] || usage; commit=$2; shift 2 ;;
    --image-repository) [[ $# -ge 2 ]] || usage; image_repository=$2; shift 2 ;;
    --builder) [[ $# -ge 2 ]] || usage; builder=$2; shift 2 ;;
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
[[ "$builder" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] || {
  echo "Buildx builder name is invalid" >&2
  exit 2
}
command -v git >/dev/null || { echo "git is required" >&2; exit 2; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 2; }
docker buildx inspect "$builder" >/dev/null

work=$(mktemp -d /tmp/luma-lae-agent-runner.XXXXXX)
cleanup() {
  rm -rf "$work"
}
trap cleanup EXIT

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
docker buildx build \
  --builder "$builder" \
  --platform linux/amd64 \
  --pull \
  --provenance=true \
  --sbom=true \
  --file "$work/lae/deploy/luma/docker/agent-runner.Dockerfile" \
  --tag "$tag" \
  --metadata-file "$metadata" \
  --push \
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
docker buildx imagetools inspect "$immutable" >/dev/null
printf '%s\n' "$immutable"
