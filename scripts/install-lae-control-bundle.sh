#!/usr/bin/env bash
# Install one coordinated LAE principal/broker/signing bundle on a Luma manager.
set -Eeuo pipefail
umask 077

[[ ${EUID} -eq 0 ]] || { echo "run as root" >&2; exit 2; }
[[ $# -eq 1 ]] || { echo "usage: $0 /path/to/lae-bundle" >&2; exit 2; }
bundle=$1
[[ -d "$bundle" && ! -L "$bundle" ]] || { echo "invalid bundle directory" >&2; exit 2; }

python3 - "$bundle" <<'PY'
import json, os, re, stat, sys
from pathlib import Path

bundle = Path(sys.argv[1]).resolve()
required = {
    "bundle-manifest.json", "credential-broker.token", "lae-admin.token",
    "lae-builder-principals.json", "lae-builder.token", "lae-control.env",
    "lae-plan-signing.json", "lae-runtime-principals.json", "lae-runtime.token",
    "object-broker.token",
}
if not required.issubset({p.name for p in bundle.iterdir()}):
    raise SystemExit("bundle is incomplete")
for name in required:
    path = bundle / name
    mode = stat.S_IMODE(path.lstat().st_mode)
    if path.is_symlink() or not path.is_file() or mode not in {0o400, 0o600}:
        raise SystemExit(f"unsafe bundle file: {name}")

manifest = json.loads((bundle / "bundle-manifest.json").read_text())
fingerprint = __import__("hashlib").sha256(
    (str(manifest.get("clusterId")) + "\0" + str(manifest.get("analyzerImageDigest"))).encode()
).hexdigest()[:16]
suffix = "-" + fingerprint
destination = Path("/opt/luma/control")
destination.mkdir(parents=True, exist_ok=True, mode=0o700)

mapping = {
    "lae-builder.token": f"lae-builder{suffix}.token",
    "lae-runtime.token": f"lae-runtime{suffix}.token",
    "credential-broker.token": f"credential-broker{suffix}.token",
    "object-broker.token": f"object-broker{suffix}.token",
    "lae-admin.token": f"lae-admin{suffix}.token",
    "lae-plan-signing.json": f"lae-plan-signing{suffix}.json",
}

def atomic(name, content):
    target = destination / name
    temporary = destination / ("." + name + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

for source, target in mapping.items():
    atomic(target, (bundle / source).read_bytes())

builder = json.loads((bundle / "lae-builder-principals.json").read_text())
builder["lae-builder"]["tokenFile"] = mapping["lae-builder.token"]
runtime = json.loads((bundle / "lae-runtime-principals.json").read_text())
runtime["lae-runtime"]["tokenFile"] = mapping["lae-runtime.token"]
builder_name = f"lae-builder-principals{suffix}.json"
runtime_name = f"lae-runtime-principals{suffix}.json"
atomic(builder_name, (json.dumps(builder, separators=(",", ":")) + "\n").encode())
atomic(runtime_name, (json.dumps(runtime, separators=(",", ":")) + "\n").encode())

control = {}
for line in (bundle / "lae-control.env").read_text().splitlines():
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*=.*", line):
        raise SystemExit("invalid control environment")
    key, value = line.split("=", 1)
    if key in control or not value:
        raise SystemExit("invalid control environment")
    control[key] = value
control.update({
    "LUMA_LAE_SERVICE_PRINCIPALS_FILE": str(destination / builder_name),
    "LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_FILE": str(destination / runtime_name),
    "LUMA_CREDENTIAL_BROKER_TOKEN_FILE": str(destination / mapping["credential-broker.token"]),
    "LUMA_OBJECT_SOURCE_BROKER_TOKEN_FILE": str(destination / mapping["object-broker.token"]),
    "LUMA_LAE_ADMIN_TOKEN_FILE": str(destination / mapping["lae-admin.token"]),
    "LUMA_LAE_PLAN_SIGNING_KEYS_FILE": str(destination / mapping["lae-plan-signing.json"]),
})
atomic("control.env", "".join(f"{k}={control[k]}\n" for k in sorted(control)).encode())
directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
try: os.fsync(directory_fd)
finally: os.close(directory_fd)
print(json.dumps({"ok": True, "bundleFingerprint": fingerprint, "controlEnvironmentKeys": sorted(control)}))
PY
