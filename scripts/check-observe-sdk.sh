#!/usr/bin/env bash
# Validate the independently installed SDK and package without touching Control.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONDONTWRITEBYTECODE=1

sdk_output=""
if [[ "$#" == 2 && "$1" == "--output-dir" ]]; then
  sdk_output="$2"
elif [[ "$#" != 0 ]]; then
  echo "Usage: bash scripts/check-observe-sdk.sh [--output-dir DIRECTORY]" >&2
  exit 2
fi

sdk_temp="$(mktemp -d "${TMPDIR:-/tmp}/luma-observe-sdk.XXXXXX")"
trap 'rm -rf "$sdk_temp"' EXIT

python -c 'import sys; assert sys.version_info >= (3, 10), "SDK checks require Python 3.10 or newer"'
python -m ruff check --no-cache --config sdk/python/pyproject.toml sdk/python/src sdk/python/tests
python -m pytest -p no:cacheprovider --basetemp "$sdk_temp/pytest" -c sdk/python/pyproject.toml sdk/python/tests

# Build from a disposable copy so setuptools metadata stays out of the checkout.
python - "$sdk_temp/source" <<'PY'
import shutil
import sys

shutil.copytree("sdk/python", sys.argv[1], ignore=shutil.ignore_patterns(
    ".venv", "__pycache__", "*.pyc", "*.egg-info", ".pytest_cache", ".ruff_cache", "dist", "build",
))
PY
python -m build "$sdk_temp/source" --outdir "$sdk_temp/dist"
python -m twine check "$sdk_temp/dist/"*
python - "$sdk_temp/dist" <<'PY'
from pathlib import Path, PurePosixPath
import sys
import tarfile
from zipfile import ZipFile

expected = Path("LICENSE").read_bytes()
assert Path("sdk/python/LICENSE").read_bytes() == expected, "SDK license differs from the repository license"
distributions = Path(sys.argv[1])
wheels = list(distributions.glob("*.whl"))
sdists = list(distributions.glob("*.tar.gz"))
assert len(wheels) == len(sdists) == 1, "expected one wheel and one source distribution"
with ZipFile(wheels[0]) as archive:
    licenses = [name for name in archive.namelist() if name.endswith(".dist-info/licenses/LICENSE")]
    assert len(licenses) == 1, "wheel must contain the SDK license"
    assert archive.read(licenses[0]) == expected, "wheel license text differs from the repository license"
with tarfile.open(sdists[0]) as archive:
    licenses = [member for member in archive.getmembers()
                if member.isfile() and len(PurePosixPath(member.name).parts) == 2
                and PurePosixPath(member.name).name == "LICENSE"]
    assert len(licenses) == 1, "source distribution must contain the SDK license"
    assert archive.extractfile(licenses[0]).read() == expected, "source distribution license text differs from the repository license"
print("License text verified in wheel and source distribution")
PY
python -m pip install --no-deps --target "$sdk_temp/installed" "$sdk_temp/dist/"*.whl

# Dependencies come from the validation environment; SDK code comes from the wheel.
(
  cd "$sdk_temp"
  PYTHONPATH="$sdk_temp/installed" LUMA_OBSERVE_ENABLED=false python - "$sdk_temp/installed" <<'PY'
import importlib.metadata
from pathlib import Path
import sys

import luma_observe
from luma_observe.dashboards import build_dashboard, write_dashboard

assert Path(luma_observe.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
assert not luma_observe.get_runtime().enabled
print("Installed wheel:", importlib.metadata.version("luma-observe-sdk"))
assert callable(build_dashboard) and callable(write_dashboard)
PY
  PYTHONPATH="$sdk_temp/installed" python -m luma_observe.dashboards --help > /dev/null
)

if [[ -n "$sdk_output" ]]; then
  mkdir -p "$sdk_output"
  cp "$sdk_temp/dist/"* "$sdk_output/"
fi
