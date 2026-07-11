#!/usr/bin/env bash
# Prepare an Ubuntu Luma node for the LAE Builder v2 executors.
#
# This script intentionally accepts no registry, Git, deploy, or Luma token.
# Builder credentials remain task-scoped and are redeemed by the node agent.

set -Eeuo pipefail

umask 022
export LC_ALL=C
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

readonly BUILDER_USER="ubuntu"
readonly BUILDER_UID="1000"
readonly BUILDKIT_VERSION="v0.31.1"
readonly SYFT_VERSION="v1.46.0"
readonly TRIVY_VERSION="v0.72.0"
readonly COSIGN_VERSION="v3.1.1"
readonly CRANE_VERSION="v0.21.7"
readonly DOCKER_MIN_VERSION="29.0.0"

readonly SYFT_CHECKSUM_FILE_SHA256="2fefc202b2eccab83888cc91f5a364a75df0dd777afbbae5b5e23ebd93d81ac6"
readonly TRIVY_CHECKSUM_FILE_SHA256="ebe9d19a774b950e240b1017a038e9b5a002ea068e02023369ff6d241c10c580"
readonly COSIGN_CHECKSUM_FILE_SHA256="47ec240858ef4c4f6d214fee9ed351c9631ee8ed3e2536ce9885a41cf509be6f"
readonly CRANE_CHECKSUM_FILE_SHA256="cd15501232e498a51ef7d2d65dd2fb360f9f1086e234acef1af02343cea291f9"

readonly BUILDKIT_ASSET="buildkit-${BUILDKIT_VERSION}.linux-amd64.tar.gz"
readonly SYFT_ASSET="syft_${SYFT_VERSION#v}_linux_amd64.tar.gz"
readonly SYFT_CHECKSUM_FILE="syft_${SYFT_VERSION#v}_checksums.txt"
readonly TRIVY_ASSET="trivy_${TRIVY_VERSION#v}_Linux-64bit.tar.gz"
readonly TRIVY_CHECKSUM_FILE="trivy_${TRIVY_VERSION#v}_checksums.txt"
readonly COSIGN_ASSET="cosign-linux-amd64"
readonly COSIGN_CHECKSUM_FILE="cosign_checksums.txt"
readonly CRANE_ASSET="go-containerregistry_Linux_x86_64.tar.gz"
readonly CRANE_CHECKSUM_FILE="checksums.txt"

readonly ENV_FILE="/etc/default/luma-node-agent"
readonly NODE_AGENT_UNIT="luma-node-agent.service"
readonly BUILDER_ROOT="/var/lib/luma/builder"
readonly WORK_ROOT="${BUILDER_ROOT}/work"
readonly SNAPSHOT_ROOT="${BUILDER_ROOT}/snapshots"
readonly TRIVY_CACHE_DIR="${BUILDER_ROOT}/trivy-cache"
readonly AUDIT_DIR="/var/log/luma"
readonly AUDIT_LOG="${AUDIT_DIR}/lae-builder-setup.log"
readonly MANIFEST_FILE="${BUILDER_ROOT}/toolchain-manifest.env"
readonly ROOTLESS_DOCKER_REGISTRY_STATE="${BUILDER_ROOT}/rootless-docker-managed-registries.json"
readonly BUILDKIT_INSTALL_ROOT="/usr/local/lib/luma-builder/buildkit-${BUILDKIT_VERSION}"
readonly BUILDKIT_USER_UNIT="luma-buildkit.service"
readonly TRIVY_DB_REPOSITORY="ghcr.io/aquasecurity/trivy-db:2"

MODE="setup"
RUNNER_IMAGE=""
REGISTRY_PULL_HOST=""
REGISTRY_PUSH_HOST=""
REGISTRY_INSECURE="0"
BUILDKIT_SHA256=""
EXTERNAL_REGISTRIES=("docker.io" "ghcr.io")
EXTERNAL_REGISTRIES_EXPLICIT="0"
TEMP_DIR=""
BIND_PROBE_DIR=""
AUDIT_READY="0"
COMPLETED="0"

usage() {
  cat <<'EOF'
Usage:
  sudo scripts/setup-lae-builder.sh [--check] \
    --runner-image IMAGE@sha256:DIGEST \
    --registry-host HOST[:PORT] \
    --registry-push-host HOST[:PORT] \
    --buildkit-sha256 SHA256 \
    [--registry-insecure] \
    [--external-registry HOST[:PORT] ...]

Required trust inputs:
  --runner-image          Exact LAE analyzer runner image digest. Tags are rejected.
  --registry-host         Builder pull registry host. No scheme, path, or credentials.
  --registry-push-host    BuildKit push registry host as seen from the builder.
  --buildkit-sha256       SHA-256 of buildkit-v0.31.1.linux-amd64.tar.gz.

Modes:
  default                 Configure the local host idempotently, then verify it.
  --check                 No persistent configuration changes: it does not install,
                          pull, refresh, or restart. It creates and removes one real
                          rootless bind probe. The same trust inputs are required.

Security properties:
  * Ubuntu user "ubuntu" must exist with UID 1000.
  * Docker Engine is not downloaded or upgraded; version 29.0.0+ is required.
  * Downloaded tools use fixed releases. BuildKit uses the operator-supplied
    asset digest; all other release assets use checksum files whose own hashes
    are pinned in this script.
  * No deploy token, registry password, or source credential is accepted or
    written. Runtime credentials remain short-lived task leases.
EOF
}

die() {
  printf 'setup-lae-builder: %s\n' "$*" >&2
  exit 1
}

audit() {
  [[ "$AUDIT_READY" == "1" ]] || return 0
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$AUDIT_LOG"
}

cleanup() {
  local status=$?
  if [[ -n "$BIND_PROBE_DIR" && -d "$BIND_PROBE_DIR" ]]; then
    rm -rf -- "$BIND_PROBE_DIR"
  fi
  if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf -- "$TEMP_DIR"
  fi
  if [[ "$AUDIT_READY" == "1" && "$COMPLETED" != "1" ]]; then
    audit "result=failed mode=${MODE} exit_code=${status}"
  fi
}
trap cleanup EXIT

require_arg() {
  local option=$1
  local value=${2-}
  [[ -n "$value" && "$value" != --* ]] || die "${option} requires a value"
}

while (($#)); do
  case "$1" in
    --check)
      MODE="check"
      shift
      ;;
    --runner-image)
      require_arg "$1" "${2-}"
      RUNNER_IMAGE=$2
      shift 2
      ;;
    --registry-host)
      require_arg "$1" "${2-}"
      REGISTRY_PULL_HOST=$2
      shift 2
      ;;
    --registry-push-host)
      require_arg "$1" "${2-}"
      REGISTRY_PUSH_HOST=$2
      shift 2
      ;;
    --buildkit-sha256)
      require_arg "$1" "${2-}"
      BUILDKIT_SHA256=${2,,}
      shift 2
      ;;
    --registry-insecure)
      REGISTRY_INSECURE="1"
      shift
      ;;
    --external-registry)
      require_arg "$1" "${2-}"
      if [[ "$EXTERNAL_REGISTRIES_EXPLICIT" == "0" ]]; then
        EXTERNAL_REGISTRIES=()
        EXTERNAL_REGISTRIES_EXPLICIT="1"
      fi
      EXTERNAL_REGISTRIES+=("$2")
      shift 2
      ;;
    -h|--help)
      usage
      COMPLETED="1"
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

validate_sha256() {
  [[ "$1" =~ ^[0-9a-f]{64}$ ]]
}

validate_registry_host() {
  local value=$1
  local label=$2
  local port=""

  [[ -n "$value" ]] || die "${label} is required"
  [[ "$value" == "${value,,}" ]] || die "${label} must be lowercase"
  [[ "$value" != *"://"* && "$value" != */* && "$value" != *@* ]] || \
    die "${label} must be a bare registry host without scheme, path, or credentials"
  [[ "$value" != *"?"* && "$value" != *"#"* && "$value" != *".."* ]] || \
    die "${label} contains invalid characters"

  if [[ "$value" =~ ^\[[0-9A-Fa-f:]+\](:([0-9]{1,5}))?$ ]]; then
    port=${BASH_REMATCH[2]-}
  elif [[ "$value" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:([0-9]{1,5}))?$ ]]; then
    port=${BASH_REMATCH[3]-}
  else
    die "${label} must be a valid DNS name, IPv4 address, or bracketed IPv6 address"
  fi
  if [[ -n "$port" ]]; then
    ((10#$port >= 1 && 10#$port <= 65535)) || die "${label} port is out of range"
  fi
}

validate_inputs() {
  [[ "$EUID" -eq 0 ]] || die "run as root (for example: sudo $0 ...)"
  [[ -n "$RUNNER_IMAGE" ]] || die "--runner-image is required"
  [[ "$RUNNER_IMAGE" =~ ^[a-z0-9][a-z0-9._:-]*(/[a-z0-9][a-z0-9._-]*)+@sha256:[0-9a-f]{64}$ ]] || \
    die "--runner-image must be an exact lowercase repository@sha256:<64 hex> reference"
  validate_registry_host "$REGISTRY_PULL_HOST" "--registry-host"
  validate_registry_host "$REGISTRY_PUSH_HOST" "--registry-push-host"
  validate_sha256 "$BUILDKIT_SHA256" || \
    die "--buildkit-sha256 must explicitly pin ${BUILDKIT_ASSET} with 64 lowercase hex characters"
  ((${#EXTERNAL_REGISTRIES[@]} > 0)) || die "at least one --external-registry is required"
  ((${#EXTERNAL_REGISTRIES[@]} <= 32)) || die "at most 32 --external-registry values are allowed"
  local host previous=""
  for host in "${EXTERNAL_REGISTRIES[@]}"; do
    validate_registry_host "$host" "--external-registry"
    [[ -z "$previous" || "$previous" < "$host" ]] || \
      die "--external-registry values must be sorted and unique"
    previous=$host
  done
}

require_ubuntu_amd64() {
  [[ "$(uname -s)" == "Linux" ]] || die "only Linux builder hosts are supported"
  case "$(uname -m)" in
    x86_64|amd64) ;;
    *) die "this pinned toolchain currently supports amd64 only" ;;
  esac
  [[ -r /etc/os-release ]] || die "/etc/os-release is missing"
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || die "this setup script requires Ubuntu"
}

require_builder_identity() {
  getent passwd "$BUILDER_USER" >/dev/null || die "user ${BUILDER_USER} does not exist"
  local actual_uid
  actual_uid=$(id -u "$BUILDER_USER")
  [[ "$actual_uid" == "$BUILDER_UID" ]] || \
    die "user ${BUILDER_USER} must have UID ${BUILDER_UID}; found ${actual_uid}"
  local home
  home=$(getent passwd "$BUILDER_USER" | awk -F: '{print $6}')
  [[ "$home" == "/home/${BUILDER_USER}" ]] || die "${BUILDER_USER} home must be /home/${BUILDER_USER}"
  [[ -d "$home" && ! -L "$home" ]] || die "${home} must be an existing real directory"
}

require_commands() {
  local missing=()
  local command_name
  for command_name in "$@"; do
    command -v "$command_name" >/dev/null 2>&1 || missing+=("$command_name")
  done
  ((${#missing[@]} == 0)) || die "missing required commands: ${missing[*]}"
}

init_audit() {
  install -d -m 0750 "$AUDIT_DIR"
  [[ ! -L "$AUDIT_LOG" ]] || die "refusing to use symlinked ${AUDIT_LOG}"
  touch "$AUDIT_LOG"
  [[ -f "$AUDIT_LOG" ]] || die "${AUDIT_LOG} must be a regular file"
  chown root:root "$AUDIT_LOG"
  chmod 0600 "$AUDIT_LOG"
  AUDIT_READY="1"
  audit "start mode=${MODE} builder_user=${BUILDER_USER} runner_image=${RUNNER_IMAGE} registry_pull=${REGISTRY_PULL_HOST} registry_push=${REGISTRY_PUSH_HOST} registry_insecure=${REGISTRY_INSECURE}"
}

install_system_dependencies() {
  require_commands apt-get
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates curl dbus-user-session fuse-overlayfs jq rootlesskit slirp4netns uidmap
  require_commands awk curl find getent install jq loginctl runuser sha256sum stat systemctl tar usermod
}

version_ge() {
  dpkg --compare-versions "$1" ge "$2"
}

docker_client_version() {
  docker version --format '{{.Client.Version}}' 2>/dev/null
}

rootless_docker_server_version() {
  run_as_builder docker --host "$(rootless_docker_host)" version --format '{{.Server.Version}}' 2>/dev/null
}

require_docker_client() {
  require_commands docker dpkg
  local version
  version=$(docker_client_version) || die "Docker CLI is unavailable"
  version_ge "$version" "$DOCKER_MIN_VERSION" || \
    die "Docker ${DOCKER_MIN_VERSION}+ is required; found ${version}. Install it through the host's trusted Docker repository."
  command -v dockerd-rootless-setuptool.sh >/dev/null 2>&1 || \
    die "dockerd-rootless-setuptool.sh is missing; install the Docker rootless extras package matching the host Docker release"
}

next_subid_start() {
  local file=$1
  awk -F: '
    BEGIN { max = 99999 }
    NF >= 3 && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ {
      end = $2 + $3 - 1
      if (end > max) max = end
    }
    END { print max + 1 }
  ' "$file"
}

ensure_subid_range() {
  local file=$1
  local option=$2
  if awk -F: -v user="$BUILDER_USER" '$1 == user && $3 + 0 >= 65536 { found = 1 } END { exit(found ? 0 : 1) }' "$file"; then
    return 0
  fi
  local start end
  start=$(next_subid_start "$file")
  end=$((start + 65535))
  usermod "$option" "${start}-${end}" "$BUILDER_USER"
  audit "configured_subid file=${file} range=${start}-${end}"
}

builder_home() {
  printf '/home/%s\n' "$BUILDER_USER"
}

builder_group() {
  id -gn "$BUILDER_USER"
}

runtime_dir() {
  printf '/run/user/%s\n' "$BUILDER_UID"
}

run_as_builder() {
  runuser -u "$BUILDER_USER" -- env \
    HOME="$(builder_home)" \
    USER="$BUILDER_USER" \
    LOGNAME="$BUILDER_USER" \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    XDG_RUNTIME_DIR="$(runtime_dir)" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=$(runtime_dir)/bus" \
    "$@"
}

wait_for_socket() {
  local socket_path=$1
  local label=$2
  local attempt
  for attempt in {1..30}; do
    [[ -S "$socket_path" ]] && return 0
    sleep 1
  done
  die "${label} socket did not become ready: ${socket_path}"
}

configure_rootless_docker() {
  ensure_subid_range /etc/subuid --add-subuids
  ensure_subid_range /etc/subgid --add-subgids
  loginctl enable-linger "$BUILDER_USER"
  systemctl start "user@${BUILDER_UID}.service"
  install -d -m 0700 -o "$BUILDER_USER" -g "$(builder_group)" "$(runtime_dir)"

  run_as_builder "$(command -v dockerd-rootless-setuptool.sh)" install --force
  configure_rootless_docker_registry
  run_as_builder systemctl --user enable docker.service
  run_as_builder systemctl --user restart docker.service
  wait_for_socket "$(runtime_dir)/docker.sock" "rootless Docker"
  audit "configured_rootless_docker socket=$(runtime_dir)/docker.sock"
}

download_file() {
  local url=$1
  local destination=$2
  curl --fail --location --silent --show-error \
    --proto '=https' --tlsv1.2 --retry 3 --retry-all-errors \
    --output "$destination" "$url"
}

verify_file_sha256() {
  local path=$1
  local expected=$2
  local actual
  actual=$(sha256sum "$path" | awk '{print $1}')
  [[ "$actual" == "$expected" ]] || \
    die "checksum mismatch for $(basename "$path"): expected ${expected}, got ${actual}"
}

checksum_for_asset() {
  local checksum_path=$1
  local asset_name=$2
  awk -v target="$asset_name" '
    {
      name = $2
      sub(/^\*/, "", name)
      sub(/^\.\//, "", name)
      if (name == target && $1 ~ /^[0-9a-fA-F]{64}$/) print tolower($1)
    }
  ' "$checksum_path"
}

download_release_asset() {
  local base_url=$1
  local asset_name=$2
  local checksum_name=$3
  local checksum_file_sha=$4
  local asset_path="${TEMP_DIR}/${asset_name}"
  local checksum_path="${TEMP_DIR}/${asset_name}.checksums"

  download_file "${base_url}/${checksum_name}" "$checksum_path"
  verify_file_sha256 "$checksum_path" "$checksum_file_sha"
  local expected
  expected=$(checksum_for_asset "$checksum_path" "$asset_name")
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || \
    die "verified checksum file does not contain exactly one checksum for ${asset_name}"
  [[ "$(checksum_for_asset "$checksum_path" "$asset_name" | wc -l | tr -d ' ')" == "1" ]] || \
    die "verified checksum file has duplicate entries for ${asset_name}"
  download_file "${base_url}/${asset_name}" "$asset_path"
  verify_file_sha256 "$asset_path" "$expected"
  printf '%s\n' "$asset_path"
}

validate_tar_members() {
  local archive=$1
  local member
  while IFS= read -r member; do
    [[ -n "$member" ]] || continue
    [[ "$member" != /* && "$member" != ".." && "$member" != ../* && "$member" != */../* && "$member" != */.. ]] || \
      die "archive contains an unsafe path: ${member}"
  done < <(tar -tzf "$archive")
}

install_binary_from_tar() {
  local archive=$1
  local binary_name=$2
  local extract_dir="${TEMP_DIR}/extract-${binary_name}"
  install -d -m 0700 "$extract_dir"
  validate_tar_members "$archive"
  tar --no-same-owner --no-same-permissions -xzf "$archive" -C "$extract_dir"
  local candidates=()
  mapfile -d '' candidates < <(find "$extract_dir" -type f -name "$binary_name" -print0)
  ((${#candidates[@]} == 1)) || die "archive must contain exactly one ${binary_name} binary"
  install -m 0755 "${candidates[0]}" "/usr/local/bin/${binary_name}"
}

install_buildkit() {
  local archive="${TEMP_DIR}/${BUILDKIT_ASSET}"
  download_file "https://github.com/moby/buildkit/releases/download/${BUILDKIT_VERSION}/${BUILDKIT_ASSET}" "$archive"
  verify_file_sha256 "$archive" "$BUILDKIT_SHA256"
  validate_tar_members "$archive"

  local extract_dir="${TEMP_DIR}/extract-buildkit"
  install -d -m 0700 "$extract_dir"
  tar --no-same-owner --no-same-permissions -xzf "$archive" -C "$extract_dir"
  [[ -x "${extract_dir}/bin/buildkitd" && -x "${extract_dir}/bin/buildctl" ]] || \
    die "verified BuildKit archive is missing buildkitd or buildctl"

  install -d -m 0755 "$BUILDKIT_INSTALL_ROOT/bin"
  local binary
  while IFS= read -r -d '' binary; do
    install -m 0755 "$binary" "${BUILDKIT_INSTALL_ROOT}/bin/$(basename "$binary")"
  done < <(find "${extract_dir}/bin" -maxdepth 1 -type f -print0)
  ln -sfn "${BUILDKIT_INSTALL_ROOT}/bin/buildctl" /usr/local/bin/buildctl
  audit "installed_tool name=buildkit version=${BUILDKIT_VERSION} asset_sha256=${BUILDKIT_SHA256}"
}

install_supply_chain_tools() {
  local asset

  asset=$(download_release_asset \
    "https://github.com/anchore/syft/releases/download/${SYFT_VERSION}" \
    "$SYFT_ASSET" "$SYFT_CHECKSUM_FILE" "$SYFT_CHECKSUM_FILE_SHA256")
  install_binary_from_tar "$asset" syft
  audit "installed_tool name=syft version=${SYFT_VERSION} asset_sha256=$(sha256sum "$asset" | awk '{print $1}')"

  asset=$(download_release_asset \
    "https://github.com/aquasecurity/trivy/releases/download/${TRIVY_VERSION}" \
    "$TRIVY_ASSET" "$TRIVY_CHECKSUM_FILE" "$TRIVY_CHECKSUM_FILE_SHA256")
  install_binary_from_tar "$asset" trivy
  audit "installed_tool name=trivy version=${TRIVY_VERSION} asset_sha256=$(sha256sum "$asset" | awk '{print $1}')"

  asset=$(download_release_asset \
    "https://github.com/sigstore/cosign/releases/download/${COSIGN_VERSION}" \
    "$COSIGN_ASSET" "$COSIGN_CHECKSUM_FILE" "$COSIGN_CHECKSUM_FILE_SHA256")
  install -m 0755 "$asset" /usr/local/bin/cosign
  audit "installed_tool name=cosign version=${COSIGN_VERSION} asset_sha256=$(sha256sum "$asset" | awk '{print $1}')"

  asset=$(download_release_asset \
    "https://github.com/google/go-containerregistry/releases/download/${CRANE_VERSION}" \
    "$CRANE_ASSET" "$CRANE_CHECKSUM_FILE" "$CRANE_CHECKSUM_FILE_SHA256")
  install_binary_from_tar "$asset" crane
  audit "installed_tool name=crane version=${CRANE_VERSION} asset_sha256=$(sha256sum "$asset" | awk '{print $1}')"
}

write_if_changed() {
  local source=$1
  local destination=$2
  local mode=$3
  local owner=$4
  local group=$5
  [[ ! -L "$destination" ]] || die "refusing to replace symlinked ${destination}"
  if [[ -f "$destination" ]] && cmp -s "$source" "$destination"; then
    rm -f -- "$source"
    return 0
  fi
  local staged="${destination}.lae-tmp.$$"
  install -m "$mode" -o "$owner" -g "$group" "$source" "$staged"
  mv -f -- "$staged" "$destination"
}

managed_insecure_registries_json() {
  if [[ "$REGISTRY_INSECURE" == "1" ]]; then
    jq -cn --arg pull "$REGISTRY_PULL_HOST" --arg push "$REGISTRY_PUSH_HOST" \
      '[$pull, $push] | unique'
  else
    printf '[]\n'
  fi
}

configure_rootless_docker_registry() {
  local docker_config_dir="$(builder_home)/.config/docker"
  local docker_config="${docker_config_dir}/daemon.json"
  install -d -m 0700 -o "$BUILDER_USER" -g "$(builder_group)" "$docker_config_dir"
  install -d -m 0750 "$BUILDER_ROOT"
  [[ ! -L "$docker_config" ]] || die "refusing to replace symlinked ${docker_config}"
  [[ ! -e "$docker_config" || -f "$docker_config" ]] || die "${docker_config} must be a regular file"

  local previous='[]'
  if [[ -f "$ROOTLESS_DOCKER_REGISTRY_STATE" ]]; then
    [[ ! -L "$ROOTLESS_DOCKER_REGISTRY_STATE" ]] || die "refusing to read symlinked ${ROOTLESS_DOCKER_REGISTRY_STATE}"
    previous=$(jq -c 'if type == "array" then . else error("managed registry state must be an array") end' \
      "$ROOTLESS_DOCKER_REGISTRY_STATE") || die "invalid ${ROOTLESS_DOCKER_REGISTRY_STATE}"
  fi
  local desired
  desired=$(managed_insecure_registries_json)
  local source="${TEMP_DIR}/rootless-docker-daemon.json"
  if [[ -f "$docker_config" ]]; then
    jq --argjson previous "$previous" --argjson desired "$desired" '
      if type != "object" then error("Docker daemon config must be an object") else . end
      | (.["insecure-registries"] // []) as $current
      | if ($current | type) != "array" then error("insecure-registries must be an array") else . end
      | .["insecure-registries"] = (($current - $previous + $desired) | unique)
    ' "$docker_config" >"$source" || die "invalid ${docker_config}"
  else
    jq -n --argjson desired "$desired" '{"insecure-registries": $desired}' >"$source"
  fi
  write_if_changed "$source" "$docker_config" 0600 "$BUILDER_USER" "$(builder_group)"

  local state_source="${TEMP_DIR}/rootless-docker-managed-registries.json"
  printf '%s\n' "$desired" >"$state_source"
  write_if_changed "$state_source" "$ROOTLESS_DOCKER_REGISTRY_STATE" 0640 root root
  audit "configured_rootless_docker_registry managed=$(tr -d '[:space:]' <"$ROOTLESS_DOCKER_REGISTRY_STATE")"
}

write_buildkit_config() {
  local config_dir="$(builder_home)/.config/buildkit"
  local config_path="${config_dir}/buildkitd.toml"
  install -d -m 0700 -o "$BUILDER_USER" -g "$(builder_group)" "$config_dir"
  local tmp="${TEMP_DIR}/buildkitd.toml"
  {
    printf '# Generated by setup-lae-builder.sh. Contains no credentials.\n'
    if [[ "$REGISTRY_INSECURE" == "1" ]]; then
      printf '[registry."%s"]\n  http = true\n  insecure = true\n' "$REGISTRY_PULL_HOST"
      if [[ "$REGISTRY_PUSH_HOST" != "$REGISTRY_PULL_HOST" ]]; then
        printf '[registry."%s"]\n  http = true\n  insecure = true\n' "$REGISTRY_PUSH_HOST"
      fi
    fi
  } >"$tmp"
  write_if_changed "$tmp" "$config_path" 0600 "$BUILDER_USER" "$(builder_group)"
}

write_buildkit_user_unit() {
  local unit_dir="$(builder_home)/.config/systemd/user"
  local unit_path="${unit_dir}/${BUILDKIT_USER_UNIT}"
  install -d -m 0700 -o "$BUILDER_USER" -g "$(builder_group)" "$unit_dir"
  local rootlesskit
  rootlesskit=$(command -v rootlesskit)
  local tmp="${TEMP_DIR}/${BUILDKIT_USER_UNIT}"
  cat >"$tmp" <<EOF
[Unit]
Description=LAE rootless BuildKit
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
RuntimeDirectory=buildkit
RuntimeDirectoryMode=0700
# Host loopback remains reachable because the existing Luma registry push
# endpoint is commonly localhost:5000; registry hosts are still explicit.
ExecStart=${rootlesskit} --net=slirp4netns --copy-up=/etc ${BUILDKIT_INSTALL_ROOT}/bin/buildkitd --config $(builder_home)/.config/buildkit/buildkitd.toml --addr unix://$(runtime_dir)/buildkit/buildkitd.sock --root $(builder_home)/.local/share/luma-buildkit
Restart=on-failure
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=default.target
EOF
  write_if_changed "$tmp" "$unit_path" 0644 "$BUILDER_USER" "$(builder_group)"
}

configure_rootless_buildkit() {
  write_buildkit_config
  write_buildkit_user_unit
  install -d -m 0700 -o "$BUILDER_USER" -g "$(builder_group)" "$(builder_home)/.local/share/luma-buildkit"
  run_as_builder systemctl --user daemon-reload
  run_as_builder systemctl --user enable "$BUILDKIT_USER_UNIT"
  run_as_builder systemctl --user restart "$BUILDKIT_USER_UNIT"
  wait_for_socket "$(runtime_dir)/buildkit/buildkitd.sock" "rootless BuildKit"
  audit "configured_rootless_buildkit socket=$(runtime_dir)/buildkit/buildkitd.sock"
}

install_trivy_database() {
  install -d -m 0750 -o "$BUILDER_USER" -g "$(builder_group)" "$TRIVY_CACHE_DIR"
  run_as_builder trivy image --download-db-only --no-progress \
    --cache-dir "$TRIVY_CACHE_DIR" --db-repository "$TRIVY_DB_REPOSITORY"
  local metadata="${TRIVY_CACHE_DIR}/db/metadata.json"
  [[ -f "$metadata" && ! -L "$metadata" ]] || die "Trivy DB metadata was not created"
  audit "updated_trivy_db repository=${TRIVY_DB_REPOSITORY} metadata_sha256=$(sha256sum "$metadata" | awk '{print $1}')"
}

rootless_docker_host() {
  printf 'unix://%s/docker.sock\n' "$(runtime_dir)"
}

pull_runner_image() {
  run_as_builder docker --host "$(rootless_docker_host)" pull "$RUNNER_IMAGE"
  verify_runner_image
  audit "pulled_runner image=${RUNNER_IMAGE}"
}

verify_runner_image() {
  local digests
  digests=$(run_as_builder docker --host "$(rootless_docker_host)" image inspect \
    --format '{{json .RepoDigests}}' "$RUNNER_IMAGE" 2>/dev/null) || \
    die "runner image is not present in rootless Docker: ${RUNNER_IMAGE}"
  grep -Fq '"'"$RUNNER_IMAGE"'"' <<<"$digests" || \
    die "local runner image does not expose the required RepoDigest: ${RUNNER_IMAGE}"
}

external_registries_json() {
  local json="["
  local separator=""
  local host
  for host in "${EXTERNAL_REGISTRIES[@]}"; do
    json+="${separator}\"${host}\""
    separator=","
  done
  json+="]"
  printf '%s\n' "$json"
}

render_node_agent_env() {
  local destination=$1
  cat >"$destination" <<EOF
# Generated by setup-lae-builder.sh. This file intentionally contains no secrets.
LUMA_BUILDER_TASKS_ENABLED=1
LUMA_BUILDER_ANALYZE_IMAGE_DIGEST=${RUNNER_IMAGE}
LUMA_BUILDER_ANALYZE_DOCKER_HOST=$(rootless_docker_host)
LUMA_BUILDER_SNAPSHOT_ROOT=${SNAPSHOT_ROOT}
LUMA_BUILDER_WORK_ROOT=${WORK_ROOT}
LUMA_BUILDER_EXTERNAL_REGISTRIES_JSON='$(external_registries_json)'
LUMA_BUILDER_BUILD_ENABLED=1
LUMA_BUILDER_BUILDKIT_ADDR=unix://$(runtime_dir)/buildkit/buildkitd.sock
LUMA_BUILDER_REGISTRY_PULL_HOST=${REGISTRY_PULL_HOST}
LUMA_BUILDER_REGISTRY_PUSH_HOST=${REGISTRY_PUSH_HOST}
LUMA_BUILDER_REGISTRY_INSECURE=${REGISTRY_INSECURE}
LUMA_BUILDER_ALLOW_ANONYMOUS_REGISTRY=1
LUMA_BUILDER_TRIVY_CACHE_DIR=${TRIVY_CACHE_DIR}
EOF
}

write_node_agent_env() {
  local tmp="${TEMP_DIR}/luma-node-agent.env"
  render_node_agent_env "$tmp"
  if grep -Eiq '^[A-Z0-9_]*(TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*=' "$tmp"; then
    die "refusing to write a credential-like key to ${ENV_FILE}"
  fi
  install -d -m 0755 /etc/default
  write_if_changed "$tmp" "$ENV_FILE" 0644 root root
  audit "wrote_builder_environment path=${ENV_FILE} sha256=$(sha256sum "$ENV_FILE" | awk '{print $1}')"
}

version_output_contains() {
  local expected=${1#v}
  shift
  "$@" 2>&1 | grep -Eq "(^|[^0-9])v?${expected//./\\.}([^0-9]|$)"
}

verify_tool_versions() {
  version_output_contains "$BUILDKIT_VERSION" buildctl --version || die "buildctl version is not ${BUILDKIT_VERSION}"
  version_output_contains "$SYFT_VERSION" syft version || die "syft version is not ${SYFT_VERSION}"
  version_output_contains "$TRIVY_VERSION" trivy --version || die "trivy version is not ${TRIVY_VERSION}"
  version_output_contains "$COSIGN_VERSION" cosign version || die "cosign version is not ${COSIGN_VERSION}"
  version_output_contains "$CRANE_VERSION" crane version || die "crane version is not ${CRANE_VERSION}"
}

verify_rootless_docker() {
  local socket_path="$(runtime_dir)/docker.sock"
  [[ -S "$socket_path" ]] || die "rootless Docker socket is missing: ${socket_path}"
  [[ "$(stat -c %u "$socket_path")" == "$BUILDER_UID" ]] || die "rootless Docker socket owner is not UID ${BUILDER_UID}"
  local security_options
  security_options=$(run_as_builder docker --host "$(rootless_docker_host)" info --format '{{json .SecurityOptions}}') || \
    die "rootless Docker daemon is unavailable"
  grep -Eiq '(^|["=])rootless([",]|$)' <<<"$security_options" || \
    die "Docker daemon did not report rootless security mode"
  local server_version
  server_version=$(rootless_docker_server_version) || die "rootless Docker server version is unavailable"
  version_ge "$server_version" "$DOCKER_MIN_VERSION" || \
    die "rootless Docker server ${DOCKER_MIN_VERSION}+ is required; found ${server_version}"
}

verify_rootless_docker_registry() {
  local docker_config="$(builder_home)/.config/docker/daemon.json"
  [[ -f "$docker_config" && ! -L "$docker_config" ]] || die "rootless Docker daemon config is missing or symlinked"
  [[ -f "$ROOTLESS_DOCKER_REGISTRY_STATE" && ! -L "$ROOTLESS_DOCKER_REGISTRY_STATE" ]] || \
    die "managed rootless Docker registry state is missing or symlinked"
  local desired state
  desired=$(managed_insecure_registries_json)
  state=$(jq -c 'if type == "array" then . | sort else error("managed registry state must be an array") end' \
    "$ROOTLESS_DOCKER_REGISTRY_STATE") || die "invalid managed registry state"
  [[ "$state" == "$(jq -c 'sort' <<<"$desired")" ]] || die "managed rootless Docker registry state does not match requested hosts"
  jq -e --argjson desired "$desired" '
    type == "object"
    and ((.["insecure-registries"] // []) | type == "array")
    and (($desired - (.["insecure-registries"] // [])) | length == 0)
  ' "$docker_config" >/dev/null || die "rootless Docker daemon config is missing a requested insecure registry"
}

verify_rootless_buildkit() {
  local socket_path="$(runtime_dir)/buildkit/buildkitd.sock"
  [[ -S "$socket_path" ]] || die "rootless BuildKit socket is missing: ${socket_path}"
  [[ "$(stat -c %u "$socket_path")" == "$BUILDER_UID" ]] || die "rootless BuildKit socket owner is not UID ${BUILDER_UID}"
  run_as_builder buildctl --addr "unix://${socket_path}" debug workers >/dev/null || \
    die "rootless BuildKit has no available worker"
  buildctl build --help 2>&1 | grep -q -- '--attest' || die "buildctl does not support attestations"
}

registry_scheme() {
  if [[ "$REGISTRY_INSECURE" == "1" ]]; then
    printf 'http\n'
  else
    printf 'https\n'
  fi
}

verify_registry() {
  local host
  for host in "$REGISTRY_PULL_HOST" "$REGISTRY_PUSH_HOST"; do
    curl --fail --silent --show-error --max-time 10 \
      --proto '=http,https' "$(registry_scheme)://${host}/v2/" >/dev/null || \
      die "registry v2 endpoint is unavailable or does not allow anonymous access: ${host}"
  done
}

verify_trivy_database() {
  local metadata="${TRIVY_CACHE_DIR}/db/metadata.json"
  [[ -f "$metadata" && ! -L "$metadata" ]] || die "Trivy DB metadata is missing: ${metadata}"
  jq -e 'type == "object" and (.UpdatedAt | type == "string")' "$metadata" >/dev/null || \
    die "Trivy DB metadata is invalid"
}

verify_storage_permissions() {
  local builder_gid
  builder_gid=$(id -g "$BUILDER_USER")
  [[ "$(stat -c '%u:%g:%a' "$BUILDER_ROOT")" == "0:${builder_gid}:710" ]] || \
    die "${BUILDER_ROOT} must be root-owned with builder-group execute-only mode 0710"
  [[ "$(stat -c '%u:%g:%a' "$WORK_ROOT")" == "0:${builder_gid}:710" ]] || \
    die "${WORK_ROOT} must be root-owned with builder-group execute-only mode 0710"
  [[ "$(stat -c '%u:%g:%a' "$SNAPSHOT_ROOT")" == "0:0:700" ]] || \
    die "${SNAPSHOT_ROOT} must remain root-owned mode 0700"
  [[ "$(stat -c '%u:%g:%a' "$TRIVY_CACHE_DIR")" == "${BUILDER_UID}:${builder_gid}:750" ]] || \
    die "${TRIVY_CACHE_DIR} ownership or mode is invalid"
}

verify_rootless_bind_probe() {
  BIND_PROBE_DIR=$(mktemp -d "${WORK_ROOT}/.lae-bind-probe.XXXXXX")
  local source_dir="${BIND_PROBE_DIR}/source"
  local input_dir="${BIND_PROBE_DIR}/input"
  local output_dir="${BIND_PROBE_DIR}/output"
  local docker_config="${BIND_PROBE_DIR}/docker-config"
  install -d -m 0700 "$source_dir" "$input_dir" "$output_dir" "$docker_config"
  printf '<!doctype html><title>LAE bind probe</title>\n' >"${source_dir}/index.html"
  cat >"${input_dir}/metadata.json" <<EOF
{"builderTaskId":"builder-bind-probe","externalOperationId":"operation-builder-bind-probe","tenantRef":"tenant-builder-bind-probe","applicationRef":"application-builder-bind-probe","resolvedCommit":"0000000000000000000000000000000000000000","sourceSnapshotId":"snapshot-builder-bind-probe","sourceSnapshotDigest":"sha256:0000000000000000000000000000000000000000000000000000000000000000","policyVersion":"builder-host-check","agentImageDigest":"${RUNNER_IMAGE}"}
EOF
  printf '{"auths":{}}\n' >"${docker_config}/config.json"
  local source_sha metadata_sha
  source_sha=$(sha256sum "${source_dir}/index.html" | awk '{print $1}')
  metadata_sha=$(sha256sum "${input_dir}/metadata.json" | awk '{print $1}')

  chown "$BUILDER_USER:$(builder_group)" \
    "$BIND_PROBE_DIR" "$source_dir" "$input_dir" "$output_dir" "$docker_config" \
    "${source_dir}/index.html" "${input_dir}/metadata.json" "${docker_config}/config.json"
  chmod 0700 "$BIND_PROBE_DIR" "$output_dir" "$docker_config"
  chmod 0500 "$source_dir" "$input_dir"
  chmod 0400 "${source_dir}/index.html" "${input_dir}/metadata.json"
  chmod 0600 "${docker_config}/config.json"

  run_as_builder env DOCKER_CONFIG="$docker_config" docker --host "$(rootless_docker_host)" run \
    --rm --pull never --user 0:0 --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 64 --memory 256m --cpus 0.25 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
    --env HOME=/tmp --env PYTHONDONTWRITEBYTECODE=1 \
    --mount "type=bind,source=${source_dir},target=/workspace,readonly" \
    --mount "type=bind,source=${input_dir},target=/input,readonly" \
    --mount "type=bind,source=${output_dir},target=/output" \
    --entrypoint lae-agent-runner "$RUNNER_IMAGE" analyze \
    --source /workspace --metadata /input/metadata.json --output-dir /output >/dev/null

  [[ "$(sha256sum "${source_dir}/index.html" | awk '{print $1}')" == "$source_sha" ]] || \
    die "rootless bind probe modified its read-only source"
  [[ "$(sha256sum "${input_dir}/metadata.json" | awk '{print $1}')" == "$metadata_sha" ]] || \
    die "rootless bind probe modified its read-only metadata"
  local result_file="${output_dir}/result.json"
  [[ -f "$result_file" && ! -L "$result_file" ]] || die "rootless bind probe did not create analyzer output"
  [[ "$(stat -c %u "$result_file")" == "$BUILDER_UID" ]] || \
    die "rootless bind probe output owner does not match the verified daemon UID"
  rm -rf -- "$BIND_PROBE_DIR"
  BIND_PROBE_DIR=""
  audit "verified_rootless_bind_probe runner=${RUNNER_IMAGE}"
}

verify_node_agent_unit() {
  systemctl cat "$NODE_AGENT_UNIT" 2>/dev/null | \
    grep -Fq 'EnvironmentFile=-/etc/default/luma-node-agent' || \
    die "${NODE_AGENT_UNIT} is missing EnvironmentFile=-/etc/default/luma-node-agent; update/install Luma first"
}

verify_node_agent_env() {
  [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || die "${ENV_FILE} is missing or is a symlink"
  local expected="${TEMP_DIR}/expected-luma-node-agent.env"
  render_node_agent_env "$expected"
  cmp -s "$expected" "$ENV_FILE" || die "${ENV_FILE} does not match the requested Builder configuration"
}

write_manifest() {
  local tmp="${TEMP_DIR}/toolchain-manifest.env"
  local docker_version
  docker_version=$(docker_client_version)
  local docker_server_version
  docker_server_version=$(rootless_docker_server_version)
  cat >"$tmp" <<EOF
# Generated by setup-lae-builder.sh; safe to archive with host audit evidence.
GENERATED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
BUILDER_USER=${BUILDER_USER}
BUILDER_UID=${BUILDER_UID}
DOCKER_CLIENT_VERSION=${docker_version}
DOCKER_ROOTLESS_SERVER_VERSION=${docker_server_version}
BUILDKIT_VERSION=${BUILDKIT_VERSION}
BUILDKIT_ASSET_SHA256=${BUILDKIT_SHA256}
BUILDCTL_BINARY_SHA256=$(sha256sum /usr/local/bin/buildctl | awk '{print $1}')
SYFT_VERSION=${SYFT_VERSION}
SYFT_BINARY_SHA256=$(sha256sum /usr/local/bin/syft | awk '{print $1}')
TRIVY_VERSION=${TRIVY_VERSION}
TRIVY_BINARY_SHA256=$(sha256sum /usr/local/bin/trivy | awk '{print $1}')
TRIVY_DB_METADATA_SHA256=$(sha256sum "${TRIVY_CACHE_DIR}/db/metadata.json" | awk '{print $1}')
COSIGN_VERSION=${COSIGN_VERSION}
COSIGN_BINARY_SHA256=$(sha256sum /usr/local/bin/cosign | awk '{print $1}')
CRANE_VERSION=${CRANE_VERSION}
CRANE_BINARY_SHA256=$(sha256sum /usr/local/bin/crane | awk '{print $1}')
RUNNER_IMAGE=${RUNNER_IMAGE}
REGISTRY_PULL_HOST=${REGISTRY_PULL_HOST}
REGISTRY_PUSH_HOST=${REGISTRY_PUSH_HOST}
REGISTRY_INSECURE=${REGISTRY_INSECURE}
NODE_AGENT_ENV_SHA256=$(sha256sum "$ENV_FILE" | awk '{print $1}')
EOF
  write_if_changed "$tmp" "$MANIFEST_FILE" 0640 root root
  audit "wrote_toolchain_manifest path=${MANIFEST_FILE} sha256=$(sha256sum "$MANIFEST_FILE" | awk '{print $1}')"
}

verify_manifest() {
  [[ -f "$MANIFEST_FILE" && ! -L "$MANIFEST_FILE" ]] || die "toolchain manifest is missing: ${MANIFEST_FILE}"
  grep -Fxq "BUILDKIT_VERSION=${BUILDKIT_VERSION}" "$MANIFEST_FILE" || die "toolchain manifest BuildKit version mismatch"
  grep -Fxq "BUILDKIT_ASSET_SHA256=${BUILDKIT_SHA256}" "$MANIFEST_FILE" || die "toolchain manifest BuildKit checksum mismatch"
  grep -Fxq "RUNNER_IMAGE=${RUNNER_IMAGE}" "$MANIFEST_FILE" || die "toolchain manifest runner mismatch"
  local name
  for name in buildctl syft trivy cosign crane; do
    local key=${name^^}
    local expected
    expected=$(sha256sum "/usr/local/bin/${name}" | awk '{print $1}')
    grep -Fxq "${key}_BINARY_SHA256=${expected}" "$MANIFEST_FILE" || die "toolchain manifest ${name} binary checksum mismatch"
  done
  local trivy_metadata_sha
  trivy_metadata_sha=$(sha256sum "${TRIVY_CACHE_DIR}/db/metadata.json" | awk '{print $1}')
  grep -Fxq "TRIVY_DB_METADATA_SHA256=${trivy_metadata_sha}" "$MANIFEST_FILE" || die "toolchain manifest Trivy DB checksum mismatch"
  local env_sha
  env_sha=$(sha256sum "$ENV_FILE" | awk '{print $1}')
  grep -Fxq "NODE_AGENT_ENV_SHA256=${env_sha}" "$MANIFEST_FILE" || die "toolchain manifest environment checksum mismatch"
}

verify_all() {
  require_commands buildctl cosign crane curl docker dpkg jq runuser sha256sum stat syft systemctl trivy
  require_docker_client
  verify_tool_versions
  verify_rootless_docker
  verify_rootless_docker_registry
  verify_rootless_buildkit
  verify_trivy_database
  verify_storage_permissions
  verify_runner_image
  verify_registry
  verify_rootless_bind_probe
  verify_node_agent_unit
  verify_node_agent_env
  verify_manifest
}

validate_inputs
require_ubuntu_amd64
require_commands awk chmod getent grep id install mktemp systemctl
require_builder_identity
verify_node_agent_unit
if [[ "$MODE" == "setup" ]]; then
  init_audit
fi
TEMP_DIR=$(mktemp -d /tmp/lae-builder-setup.XXXXXX)
chmod 0700 "$TEMP_DIR"

if [[ "$MODE" == "check" ]]; then
  verify_all
  audit "result=success mode=check"
  COMPLETED="1"
  printf 'LAE Builder host check passed.\n'
  exit 0
fi

require_docker_client
install_system_dependencies
require_docker_client
configure_rootless_docker
install_buildkit
install_supply_chain_tools
verify_tool_versions
configure_rootless_buildkit
install -d -m 0710 -o root -g "$(builder_group)" "$BUILDER_ROOT" "$WORK_ROOT"
install -d -m 0700 -o root -g root "$SNAPSHOT_ROOT"
install_trivy_database
pull_runner_image
write_node_agent_env
verify_node_agent_unit
write_manifest
systemctl restart "$NODE_AGENT_UNIT"
verify_all

audit "result=success mode=setup"
COMPLETED="1"
printf 'LAE Builder host setup completed and verified.\n'
