"""Bounded, automatic disk hygiene for Luma nodes.

The node agent runs this once per interval (daily by default) and on demand
through the ``prune-node-disk`` agent task. Each run:

* removes Docker images that no container uses and that are older than the
  configured age (seven days by default);
* prunes build cache that has not been used for the same age;
* on Linux, keeps a journald drop-in that caps the persistent journal.

Image selection is deliberately conservative. An image is kept when any
container (running or stopped) uses it, when a Nomad allocation on this node
references it, when it is the Control image of this Luma release, when the
caller protects it explicitly, or when its age cannot be established.

The module only uses the standard library so the same code can run as a
read-only report on a host that does not have this Luma version installed:
``python3 disk_hygiene.py --dry-run``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping

DISK_HYGIENE_CAPABILITY = "node-disk-hygiene-v1"
DISK_HYGIENE_ACTION = "prune-node-disk"
DEFAULT_INTERVAL_HOURS = 24
DEFAULT_MIN_AGE_DAYS = 7
DEFAULT_JOURNALD_MAX_USE = "500M"
JOURNALD_DROPIN_PATH = "/etc/systemd/journald.conf.d/luma.conf"
STATUS_FILE_NAME = "disk-hygiene.json"
LUMA_BUILDX_BUILDERS = ("luma-builder", "luma-builder-egress")
# Task results are stored by Control; keep per-image detail bounded.
MAX_REPORTED_IMAGES = 200
_INSPECT_BATCH = 50

# One cleanup at a time per process: the scheduled run and a manual task must
# never race each other's image removals.
DISK_HYGIENE_LOCK = threading.Lock()

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class DiskHygieneSettings:
    enabled: bool = True
    interval_hours: int = DEFAULT_INTERVAL_HOURS
    min_age_days: int = DEFAULT_MIN_AGE_DAYS
    build_cache: bool = True
    # Empty disables the journald cap.
    journald_max_use: str = DEFAULT_JOURNALD_MAX_USE

    def public(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "intervalHours": self.interval_hours,
            "minAgeDays": self.min_age_days,
            "buildCache": self.build_cache,
            "journaldMaxUse": self.journald_max_use,
        }


_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


def _bool_setting(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in _FALSE_VALUES:
        return False
    if text in _TRUE_VALUES:
        return True
    return default


def int_setting(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or value in (None, ""):
        return default
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def journald_max_use_value(value: Any) -> str:
    """Validate a journald size such as ``500M`` or ``2G``; empty disables."""
    text = str(value if value is not None else "").strip().upper()
    if text in {"", "OFF", "0", "NONE", "FALSE"}:
        return ""
    if not re.fullmatch(r"[1-9][0-9]{0,5}[KMGT]?", text):
        raise ValueError(f"invalid journald size: {value!r}; use a value like 500M or 2G")
    return text


def resolve_settings(config: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None) -> DiskHygieneSettings:
    """Merge agent.json ``diskHygiene`` with ``LUMA_DISK_HYGIENE_*`` overrides."""
    raw = config.get("diskHygiene") if isinstance(config, Mapping) else None
    section: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    environ = os.environ if env is None else env
    enabled = _bool_setting(section.get("enabled"), True)
    enabled = _bool_setting(environ.get("LUMA_DISK_HYGIENE"), enabled)
    interval = int_setting(section.get("intervalHours"), DEFAULT_INTERVAL_HOURS, minimum=1, maximum=24 * 30)
    interval = int_setting(environ.get("LUMA_DISK_HYGIENE_INTERVAL_HOURS"), interval, minimum=1, maximum=24 * 30)
    min_age = int_setting(section.get("minAgeDays"), DEFAULT_MIN_AGE_DAYS, minimum=1, maximum=365)
    min_age = int_setting(environ.get("LUMA_DISK_HYGIENE_MIN_AGE_DAYS"), min_age, minimum=1, maximum=365)
    build_cache = _bool_setting(section.get("buildCache"), True)
    build_cache = _bool_setting(environ.get("LUMA_DISK_HYGIENE_BUILD_CACHE"), build_cache)
    journald_raw: Any = section.get("journaldMaxUse", DEFAULT_JOURNALD_MAX_USE)
    if environ.get("LUMA_JOURNALD_MAX_USE") is not None:
        journald_raw = environ.get("LUMA_JOURNALD_MAX_USE")
    try:
        journald = journald_max_use_value(journald_raw)
    except ValueError:
        journald = DEFAULT_JOURNALD_MAX_USE
    return DiskHygieneSettings(
        enabled=enabled,
        interval_hours=interval,
        min_age_days=min_age,
        build_cache=build_cache,
        journald_max_use=journald,
    )


# ---------------------------------------------------------------------------
# Image references and selection


@dataclass(frozen=True)
class ImageRecord:
    id: str
    repo_tags: tuple[str, ...] = ()
    repo_digests: tuple[str, ...] = ()
    created: float = 0.0
    last_tag_time: float = 0.0
    size: int = 0

    @property
    def refs(self) -> tuple[str, ...]:
        return self.repo_tags + self.repo_digests

    def removal_refs(self) -> list[str]:
        # Remove by name, never with --force: a container that appears after
        # the scan makes Docker refuse the last untag instead of orphaning it.
        if self.repo_tags:
            return list(self.repo_tags)
        if self.repo_digests:
            return list(self.repo_digests)
        return [self.id]


_DOCKER_HUB_REGISTRIES = {"docker.io", "index.docker.io", "registry-1.docker.io"}


def image_ref_keys(ref: str) -> set[str]:
    """Canonical comparison keys for an image reference.

    ``nginx``, ``nginx:latest`` and ``docker.io/library/nginx:latest`` share a
    key; a ``name:tag@sha256:...`` reference yields both its tag and digest key.
    """
    value = str(ref or "").strip()
    if not value or "${" in value or any(ch.isspace() for ch in value):
        return set()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        return {value}
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return {f"sha256:{value}"}
    name, _, digest = value.partition("@")
    tag = ""
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        name, tag = name[:colon], name[colon + 1:]
    if not name:
        return set()
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, path = parts[0].lower(), "/".join(parts[1:])
    else:
        registry, path = "docker.io", name
    if registry in _DOCKER_HUB_REGISTRIES:
        registry = "docker.io"
        if "/" not in path:
            path = f"library/{path}"
    repository = f"{registry}/{path.lower()}"
    keys: set[str] = set()
    if digest:
        keys.add(f"{repository}@{digest.lower()}")
    if tag or not digest:
        keys.add(f"{repository}:{tag or 'latest'}")
    return keys


def _image_keys(image: ImageRecord) -> set[str]:
    keys = {image.id}
    for ref in image.refs:
        keys.update(image_ref_keys(ref))
    return keys


def _refs_keys(refs: Iterable[str]) -> set[str]:
    keys: set[str] = set()
    for ref in refs:
        keys.update(image_ref_keys(ref))
    return keys


# Images that Nomad or Luma start on demand rather than through a job, so no
# container or allocation references them between uses. Removing one makes the
# next bridge allocation or host repair pull it again, which fails on nodes
# whose network cannot reach the upstream registry.
RUNTIME_REPOSITORIES = (
    "registry.k8s.io/pause",      # Nomad's bridge-network infra container
    "gcr.io/google_containers/pause",
)
RUNTIME_IMAGES = (
    "ubuntu:22.04",               # Control host-prep fallback (LUMA_HOST_PREP_IMAGE)
    "registry:2",                 # managed registry
    "python:3.13-alpine",         # macOS Compose network helper
)


def is_runtime_image(image: ImageRecord, extra_refs: Iterable[str] = ()) -> bool:
    keys = _image_keys(image)
    if keys & _refs_keys([*RUNTIME_IMAGES, *extra_refs]):
        return True
    for key in keys:
        if key.startswith("sha256:"):
            continue
        repository = key.split("@", 1)[0]
        head, _, tail = repository.rpartition("/")
        repository = f"{head}/{tail.split(':', 1)[0]}"
        # pause, pause-amd64, pause-arm64 ...
        if any(repository == prefix or repository.startswith(prefix + "-") for prefix in RUNTIME_REPOSITORIES):
            return True
    return False


def is_control_image(image: ImageRecord, control_tags: Iterable[str]) -> bool:
    """A ``luma-control`` image of the given release tag, from any registry or mirror."""
    tags = {str(tag).strip().lower() for tag in control_tags if str(tag).strip()}
    if not tags:
        return False
    for ref in image.repo_tags:
        for key in image_ref_keys(ref):
            repository, _, tag = key.rpartition(":")
            if repository.rsplit("/", 1)[-1] == "luma-control" and tag in tags:
                return True
    return False


def parse_docker_time(value: Any) -> float:
    """Parse Docker's RFC 3339 timestamps; the zero time and garbage give 0."""
    text = str(value or "").strip()
    match = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?(?:\s+\w+)?",
        text,
    )
    if not match:
        return 0.0
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    if int(year) <= 1970:
        return 0.0
    try:
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    seconds = moment.timestamp() + (float(f"0.{fraction}") if fraction else 0.0)
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        seconds -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return seconds


def image_from_inspect(raw: Mapping[str, Any]) -> ImageRecord | None:
    image_id = str(raw.get("Id") or "").strip()
    if not image_id:
        return None
    metadata = raw.get("Metadata") if isinstance(raw.get("Metadata"), Mapping) else {}
    tags = tuple(str(item) for item in raw.get("RepoTags") or [] if str(item) and str(item) != "<none>:<none>")
    digests = tuple(str(item) for item in raw.get("RepoDigests") or [] if str(item) and not str(item).startswith("<none>@"))
    try:
        size = int(raw.get("Size") or 0)
    except (TypeError, ValueError):
        size = 0
    return ImageRecord(
        id=image_id,
        repo_tags=tags,
        repo_digests=digests,
        created=parse_docker_time(raw.get("Created")),
        last_tag_time=parse_docker_time(metadata.get("LastTagTime")),
        size=max(size, 0),
    )


@dataclass
class ImageSelection:
    remove: list[ImageRecord] = field(default_factory=list)
    kept: Dict[str, list[ImageRecord]] = field(default_factory=dict)

    def keep(self, reason: str, image: ImageRecord) -> None:
        self.kept.setdefault(reason, []).append(image)

    def kept_counts(self) -> Dict[str, int]:
        return {reason: len(images) for reason, images in sorted(self.kept.items())}


KEEP_REASONS = ("container", "nomad", "control", "protected", "recent", "unknown-age")


def image_reference_time(image: ImageRecord, first_seen: Mapping[str, float] | None = None) -> float:
    """When the image last became relevant on this host.

    The classic image store records ``LastTagTime`` on pull and tag. The
    containerd store leaves it zero, and ``Created`` is the build time, which
    can be months before an image was pulled here. In that case the time the
    agent first saw the image stands in for the pull time (``0`` means it was
    already present when tracking started).
    """
    if image.last_tag_time:
        return max(image.created, image.last_tag_time)
    seen = float((first_seen or {}).get(image.id) or 0)
    if seen:
        return max(image.created, seen)
    return image.created


def select_images(
    images: Iterable[ImageRecord],
    *,
    container_refs: Iterable[str],
    nomad_refs: Iterable[str],
    protected_refs: Iterable[str] = (),
    control_tags: Iterable[str] = (),
    now: float,
    min_age_seconds: float,
    first_seen: Mapping[str, float] | None = None,
    runtime_refs: Iterable[str] = (),
) -> ImageSelection:
    """Split images into removal candidates and kept images with a reason."""
    container_keys = _refs_keys(container_refs)
    nomad_keys = _refs_keys(nomad_refs)
    protected_keys = _refs_keys(protected_refs)
    selection = ImageSelection()
    for image in images:
        keys = _image_keys(image)
        if keys & container_keys:
            selection.keep("container", image)
        elif keys & nomad_keys:
            selection.keep("nomad", image)
        elif is_control_image(image, control_tags):
            selection.keep("control", image)
        elif keys & protected_keys or is_runtime_image(image, runtime_refs):
            selection.keep("protected", image)
        else:
            reference = image_reference_time(image, first_seen)
            if reference <= 0:
                selection.keep("unknown-age", image)
            elif now - reference < min_age_seconds:
                selection.keep("recent", image)
            else:
                selection.remove.append(image)
    # Newest first: an image built FROM another local image must go before
    # its parent, otherwise Docker refuses the parent.
    selection.remove.sort(key=lambda item: image_reference_time(item, first_seen), reverse=True)
    return selection


# ---------------------------------------------------------------------------
# Collection from Docker and Nomad


def find_docker_binary() -> str | None:
    docker = shutil.which("docker")
    if docker:
        return docker
    for candidate in (
        "/usr/local/bin/docker",
        "/opt/homebrew/bin/docker",
        "/Applications/OrbStack.app/Contents/MacOS/xbin/docker",
        "/Applications/Docker.app/Contents/Resources/bin/docker",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def _run(runner: Runner, args: list[str], *, timeout: int, env: Mapping[str, str] | None = None) -> "subprocess.CompletedProcess[str]":
    return runner(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
        env=dict(env) if env is not None else None,
    )


def _output_error(result: "subprocess.CompletedProcess[str]") -> str:
    text = (result.stderr or result.stdout or "").strip()
    return text.splitlines()[-1][:300] if text else f"exit status {result.returncode}"


def _unique_lines(output: str) -> list[str]:
    return list(dict.fromkeys(line.strip() for line in output.splitlines() if line.strip()))


def _batches(items: list[str], size: int = _INSPECT_BATCH) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def list_image_ids(docker: str, *, runner: Runner = subprocess.run) -> list[str]:
    """Top-level image IDs, including untagged (dangling) ones.

    Docker 29 hides dangling images from a plain ``image ls``; ``-a`` would
    add the classic builder's intermediate layers, which always have children.
    """
    ids: list[str] = []
    for extra in ([], ["--filter", "dangling=true"]):
        listed = _run(runner, [docker, "image", "ls", "-q", "--no-trunc", *extra], timeout=60)
        if listed.returncode != 0:
            raise RuntimeError(f"docker image ls failed: {_output_error(listed)}")
        ids.extend(_unique_lines(listed.stdout))
    return list(dict.fromkeys(ids))


def collect_images(docker: str, *, runner: Runner = subprocess.run) -> list[ImageRecord]:
    listed = list_image_ids(docker, runner=runner)
    images: Dict[str, ImageRecord] = {}
    for batch in _batches(listed):
        inspected = _run(runner, [docker, "image", "inspect", *batch], timeout=60)
        try:
            rows = json.loads(inspected.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"docker image inspect returned invalid JSON: {_output_error(inspected)}") from exc
        # An image removed between ls and inspect only drops itself.
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, Mapping):
                image = image_from_inspect(row)
                if image is not None:
                    images[image.id] = image
    return list(images.values())


def collect_container_refs(docker: str, *, runner: Runner = subprocess.run) -> set[str]:
    """Image IDs and names used by every container, running or stopped."""
    listed = _run(runner, [docker, "ps", "-aq", "--no-trunc"], timeout=60)
    if listed.returncode != 0:
        raise RuntimeError(f"docker ps failed: {_output_error(listed)}")
    refs: set[str] = set()
    for batch in _batches(_unique_lines(listed.stdout)):
        inspected = _run(
            runner,
            [docker, "container", "inspect", "--format", "{{.Image}}\t{{.Config.Image}}", *batch],
            timeout=60,
        )
        lines = [line for line in (inspected.stdout or "").splitlines() if line.strip()]
        if inspected.returncode != 0 and not lines:
            raise RuntimeError(f"docker container inspect failed: {_output_error(inspected)}")
        for line in lines:
            refs.update(part.strip() for part in line.split("\t") if part.strip())
    return refs


def _nomad_get(addr: str, path: str, *, timeout: int, token: str) -> Any:
    request = urllib.request.Request(f"{addr.rstrip('/')}{path}")
    if token:
        request.add_header("X-Nomad-Token", token)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local Nomad agent
        return json.loads(response.read().decode("utf-8"))


_TERMINAL_CLIENT_STATUSES = {"complete", "failed", "lost"}


def nomad_allocation_image_refs(allocations: Iterable[Mapping[str, Any]]) -> set[str]:
    """Docker images referenced by allocations that may still start a task."""
    refs: set[str] = set()
    for alloc in allocations:
        if not isinstance(alloc, Mapping):
            continue
        client_status = str(alloc.get("ClientStatus") or "").lower()
        desired_status = str(alloc.get("DesiredStatus") or "").lower()
        if client_status in _TERMINAL_CLIENT_STATUSES and desired_status != "run":
            continue
        job = alloc.get("Job") if isinstance(alloc.get("Job"), Mapping) else {}
        group_name = str(alloc.get("TaskGroup") or "")
        for group in job.get("TaskGroups") or []:
            if not isinstance(group, Mapping):
                continue
            if group_name and str(group.get("Name") or "") != group_name:
                continue
            for task in group.get("Tasks") or []:
                if not isinstance(task, Mapping) or str(task.get("Driver") or "") != "docker":
                    continue
                config = task.get("Config") if isinstance(task.get("Config"), Mapping) else {}
                image = str(config.get("image") or "").strip()
                if image:
                    refs.add(image)
    return refs


def collect_nomad_refs(addr: str = "", *, timeout: int = 10, token: str | None = None) -> set[str]:
    """Images referenced by this node's Nomad allocations (local client API)."""
    address = addr or os.environ.get("NOMAD_ADDR") or "http://127.0.0.1:4646"
    nomad_token = os.environ.get("NOMAD_TOKEN", "") if token is None else token
    agent = _nomad_get(address, "/v1/agent/self", timeout=timeout, token=nomad_token)
    stats = agent.get("stats") if isinstance(agent, Mapping) else {}
    client = stats.get("client") if isinstance(stats, Mapping) else {}
    node_id = str(client.get("node_id") or "").strip() if isinstance(client, Mapping) else ""
    if not node_id:
        raise RuntimeError("local Nomad agent did not report a client node ID")
    allocations = _nomad_get(
        address, f"/v1/node/{urllib.parse.quote(node_id)}/allocations", timeout=timeout, token=nomad_token
    )
    if not isinstance(allocations, list):
        raise RuntimeError("Nomad returned an invalid allocation list")
    return nomad_allocation_image_refs(allocations)


# ---------------------------------------------------------------------------
# Sizes


_SIZE_UNITS = {
    "b": 1,
    "kb": 1000,
    "kib": 1024,
    "mb": 1000 ** 2,
    "mib": 1024 ** 2,
    "gb": 1000 ** 3,
    "gib": 1024 ** 3,
    "tb": 1000 ** 4,
    "tib": 1024 ** 4,
}


def parse_size(value: str) -> int:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)", str(value or "").strip())
    if not match:
        return 0
    scale = _SIZE_UNITS.get(match.group(2).lower())
    return int(float(match.group(1)) * scale) if scale else 0


def docker_images_disk_bytes(docker: str, *, runner: Runner = subprocess.run) -> int | None:
    result = _run(runner, [docker, "system", "df", "--format", "{{.Type}}\t{{.Size}}"], timeout=120)
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        kind, _, size = line.partition("\t")
        if kind.strip() == "Images":
            return parse_size(size)
    return None


def docker_image_df_sizes(docker: str, *, runner: Runner = subprocess.run) -> Dict[str, tuple[int, int]]:
    """``(size, unique size)`` by image ID from ``docker system df -v``.

    These figures come from one source, so the unique size never exceeds the
    size; ``image inspect`` sizes are measured differently on the containerd
    store. Empty on Docker versions without JSON output.
    """
    result = _run(runner, [docker, "system", "df", "-v", "--format", "json"], timeout=180)
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    rows = payload.get("Images") if isinstance(payload, Mapping) else None
    sizes: Dict[str, tuple[int, int]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        image_id = str(row.get("ID") or "")
        if image_id and not image_id.startswith("sha256:"):
            image_id = f"sha256:{image_id}"
        if image_id:
            sizes[image_id] = (parse_size(str(row.get("Size") or "")), parse_size(str(row.get("UniqueSize") or "")))
    return sizes


def docker_build_cache_reclaimable(docker: str, *, runner: Runner = subprocess.run) -> tuple[int, str]:
    """Reclaimable default-builder cache; works without the buildx plugin."""
    result = _run(runner, [docker, "system", "df", "--format", "{{.Type}}\t{{.Reclaimable}}"], timeout=120)
    if result.returncode != 0:
        return 0, _output_error(result)
    for line in (result.stdout or "").splitlines():
        kind, _, value = line.partition("\t")
        if kind.strip() == "Build Cache":
            return parse_size(value.split("(")[0]), ""
    return 0, ""


def _reclaimed_from_prune_output(output: str) -> int:
    match = re.search(r"Total(?: reclaimed space)?:\s*([0-9.]+\s*[A-Za-z]+)", output or "")
    return parse_size(match.group(1)) if match else 0


def _reclaimable_from_du_output(output: str) -> int:
    match = re.search(r"Reclaimable:\s*([0-9.]+\s*[A-Za-z]+)", output or "")
    return parse_size(match.group(1)) if match else 0


def format_bytes(value: int) -> str:
    amount = float(max(int(value or 0), 0))
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1000:
            return f"{amount:.0f}{unit}" if unit == "B" else f"{amount:.1f}{unit}"
        amount /= 1000
    return f"{amount:.1f}TB"


# ---------------------------------------------------------------------------
# Phases


def _image_report(image: ImageRecord, *, now: float, first_seen: Mapping[str, float] | None) -> Dict[str, Any]:
    reference = image_reference_time(image, first_seen)
    return {
        "id": image.id,
        "refs": list(image.refs)[:8],
        "sizeBytes": image.size,
        "ageDays": round((now - reference) / 86400, 1) if reference else None,
    }


def prune_images(
    docker: str,
    *,
    min_age_days: int,
    dry_run: bool,
    protected_refs: Iterable[str] = (),
    control_tags: Iterable[str] = (),
    nomad_refs: Callable[[], set[str]] | None = None,
    first_seen: Mapping[str, float] | None = None,
    now: float | None = None,
    runner: Runner = subprocess.run,
) -> Dict[str, Any]:
    moment = time.time() if now is None else now
    images = collect_images(docker, runner=runner)
    containers = collect_container_refs(docker, runner=runner)
    try:
        nomad = (nomad_refs or collect_nomad_refs)()
    except (OSError, urllib.error.URLError, RuntimeError, ValueError) as exc:
        # Fail closed: without the allocation list an image that is about to
        # start would look unused.
        return {
            "status": "skipped",
            "reason": f"Nomad allocations could not be read, so no image was removed: {exc}",
            "imagesScanned": len(images),
        }
    selection = select_images(
        images,
        container_refs=containers,
        nomad_refs=nomad,
        protected_refs=protected_refs,
        control_tags=control_tags,
        now=moment,
        min_age_seconds=min_age_days * 86400,
        first_seen=first_seen,
        runtime_refs=[value for value in (os.environ.get("LUMA_HOST_PREP_IMAGE", "").strip(),) if value],
    )
    report: Dict[str, Any] = {
        "status": "ok",
        "imagesScanned": len(images),
        "kept": selection.kept_counts(),
        "candidates": [_image_report(item, now=moment, first_seen=first_seen) for item in selection.remove[:MAX_REPORTED_IMAGES]],
        "candidateCount": len(selection.remove),
        # Upper bound: candidates can share layers with each other or with kept images.
        "estimatedBytes": sum(item.size for item in selection.remove),
        "removed": [],
        "failed": [],
        "reclaimedBytes": 0,
    }
    if dry_run and selection.remove:
        sizes = docker_image_df_sizes(docker, runner=runner)
        if sizes and all(item.id in sizes for item in selection.remove):
            for entry in report["candidates"]:
                entry["sizeBytes"] = sizes[entry["id"]][0]
            report["estimatedBytes"] = sum(sizes[item.id][0] for item in selection.remove)
            # Lower bound: layers shared only among candidates count for none of them.
            report["uniqueBytes"] = sum(sizes[item.id][1] for item in selection.remove)
    if dry_run or not selection.remove:
        return report
    before = docker_images_disk_bytes(docker, runner=runner)
    removed: list[str] = []
    failed: list[Dict[str, str]] = []
    for image in selection.remove:
        result = _run(runner, [docker, "image", "rm", *image.removal_refs()], timeout=180)
        if result.returncode == 0:
            removed.append(image.id)
        else:
            failed.append({"id": image.id, "error": _output_error(result)})
    after = docker_images_disk_bytes(docker, runner=runner)
    report["removed"] = removed[:MAX_REPORTED_IMAGES]
    report["removedCount"] = len(removed)
    report["failed"] = failed[:MAX_REPORTED_IMAGES]
    if before is not None and after is not None:
        report["reclaimedBytes"] = max(before - after, 0)
    if failed and not removed:
        report["status"] = "failed"
    elif failed:
        report["status"] = "partial"
    return report


def _builder_running(docker: str, name: str, *, runner: Runner) -> bool:
    result = _run(
        runner,
        [docker, "ps", "-q", "--filter", f"name=^buildx_buildkit_{name}0$"],
        timeout=30,
    )
    return result.returncode == 0 and bool((result.stdout or "").strip())


def prune_build_cache(
    docker: str,
    *,
    min_age_days: int,
    dry_run: bool,
    buildx_config_root: Path | None = None,
    runner: Runner = subprocess.run,
) -> Dict[str, Any]:
    """Prune build cache unused for ``min_age_days`` in the default and Luma builders."""
    until = f"until={min_age_days * 24}h"
    targets: list[tuple[str, list[str], Mapping[str, str] | None]] = [("default", ["builder"], None)]
    if buildx_config_root is not None:
        env = {**os.environ, "BUILDX_CONFIG": str(buildx_config_root)}
        for name in LUMA_BUILDX_BUILDERS:
            # Pruning a stopped docker-container builder would boot it.
            if (buildx_config_root / "instances" / name).exists() and _builder_running(docker, name, runner=runner):
                targets.append((name, ["buildx", "--builder", name], env))
    builders: list[Dict[str, Any]] = []
    for name, prefix, env in targets:
        if dry_run:
            # BuildKit only reports last use relative to now, so the dry run
            # shows the builder's total reclaimable cache as an upper bound.
            if name == "default":
                reclaimable, error = docker_build_cache_reclaimable(docker, runner=runner)
                item: Dict[str, Any] = {"name": name, "reclaimableBytes": reclaimable}
                if error:
                    item["error"] = error
                builders.append(item)
                continue
            result = _run(runner, [docker, *prefix, "du"], timeout=120, env=env)
            item = {"name": name, "reclaimableBytes": _reclaimable_from_du_output(result.stdout)}
        else:
            result = _run(runner, [docker, *prefix, "prune", "-f", "--filter", until], timeout=900, env=env)
            item = {"name": name, "reclaimedBytes": _reclaimed_from_prune_output(result.stdout)}
        if result.returncode != 0:
            item["error"] = _output_error(result)
        builders.append(item)
    failed = [item for item in builders if item.get("error")]
    return {
        "status": "failed" if failed and len(failed) == len(builders) else "partial" if failed else "ok",
        "filter": until,
        "builders": builders,
        "reclaimedBytes": sum(int(item.get("reclaimedBytes") or 0) for item in builders),
    }


def journald_limit_script(max_use: str, *, check_only: bool = False) -> str:
    """Idempotent shell that caps the persistent journal with a drop-in.

    Prints one ``journald-limit: <state>`` line: ``unchanged``, ``changed``,
    ``would-change`` (check only) or ``skipped`` (no systemd journald).
    """
    value = journald_max_use_value(max_use)
    if not value:
        raise ValueError("journald limit is disabled")
    target = JOURNALD_DROPIN_PATH
    desired = f"# Managed by Luma: cap the persistent systemd journal.\n[Journal]\nSystemMaxUse={value}"
    lines = [
        "set -eu",
        f"target={target}",
        "if [ ! -d /run/systemd/system ] || ! command -v journalctl >/dev/null 2>&1; then echo 'journald-limit: skipped'; exit 0; fi",
        f"desired='{desired}'",
        'if [ -f "$target" ] && [ "$(cat "$target")" = "$desired" ]; then echo \'journald-limit: unchanged\';',
    ]
    if check_only:
        lines.append("else echo 'journald-limit: would-change'; fi")
    else:
        lines += [
            "else",
            f"  install -d -m 755 {os.path.dirname(target)}",
            '  tmp="$target.luma-tmp"',
            "  printf '%s\\n' \"$desired\" > \"$tmp\"",
            '  chmod 644 "$tmp"',
            '  mv -f "$tmp" "$target"',
            # journald reads its configuration at start; a restart keeps the
            # socket units, so no log line is lost.
            "  systemctl restart systemd-journald",
            "  echo 'journald-limit: changed'",
            "fi",
            # Enforce the cap now instead of on the next rotation.
            f"journalctl --vacuum-size={value} >/dev/null 2>&1 || true",
        ]
    return "\n".join(lines) + "\n"


def parse_journald_limit_state(output: str) -> str:
    for line in reversed((output or "").splitlines()):
        match = re.search(r"journald-limit:\s*([a-z-]+)", line)
        if match:
            return match.group(1)
    return "unknown"


def ensure_journald_limit(max_use: str, *, dry_run: bool, runner: Runner = subprocess.run) -> Dict[str, Any]:
    """Apply the journald cap locally; the caller must already be root."""
    try:
        script = journald_limit_script(max_use, check_only=dry_run)
    except ValueError as exc:
        return {"status": "skipped", "reason": str(exc)}
    if os.name != "posix" or not os.path.exists("/run/systemd/system"):
        return {"status": "skipped", "reason": "systemd journald is not running on this host"}
    if not dry_run and hasattr(os, "geteuid") and os.geteuid() != 0:
        return {"status": "skipped", "reason": "writing the journald drop-in requires root"}
    result = _run(runner, ["/bin/sh", "-c", script], timeout=120)
    if result.returncode != 0:
        return {"status": "failed", "maxUse": max_use, "error": _output_error(result)}
    return {"status": parse_journald_limit_state(result.stdout), "maxUse": max_use}


# ---------------------------------------------------------------------------
# Orchestration, status and scheduling


def run_disk_hygiene(
    *,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
    dry_run: bool = True,
    build_cache: bool = True,
    journald_max_use: str = DEFAULT_JOURNALD_MAX_USE,
    protected_refs: Iterable[str] = (),
    control_tags: Iterable[str] = (),
    docker: str | None = None,
    os_name: str = "",
    buildx_config_root: Path | None = None,
    nomad_refs: Callable[[], set[str]] | None = None,
    first_seen: Mapping[str, float] | None = None,
    progress: Callable[[str], None] | None = None,
    runner: Runner = subprocess.run,
) -> Dict[str, Any]:
    """Run every phase; a failing phase is reported and never stops the others."""
    started = time.time()
    docker_cli = docker or find_docker_binary()
    system = os_name or ("darwin" if os.uname().sysname == "Darwin" else os.uname().sysname.lower())

    def emit(message: str) -> None:
        if progress:
            progress(message)

    result: Dict[str, Any] = {"dryRun": dry_run, "minAgeDays": min_age_days, "startedAt": int(started)}
    if not docker_cli:
        result["images"] = {"status": "skipped", "reason": "docker command not found"}
        result["buildCache"] = {"status": "skipped", "reason": "docker command not found"}
    else:
        emit("Scanning Docker images")
        try:
            result["images"] = prune_images(
                docker_cli,
                min_age_days=min_age_days,
                dry_run=dry_run,
                protected_refs=protected_refs,
                control_tags=control_tags,
                nomad_refs=nomad_refs,
                first_seen=first_seen,
                now=started,
                runner=runner,
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            result["images"] = {"status": "failed", "error": str(exc)}
        if build_cache:
            emit("Pruning build cache" if not dry_run else "Measuring build cache")
            try:
                result["buildCache"] = prune_build_cache(
                    docker_cli,
                    min_age_days=min_age_days,
                    dry_run=dry_run,
                    buildx_config_root=buildx_config_root,
                    runner=runner,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                result["buildCache"] = {"status": "failed", "error": str(exc)}
        else:
            result["buildCache"] = {"status": "skipped", "reason": "disabled"}
    if system == "linux" and journald_max_use:
        emit("Checking journald size limit")
        try:
            result["journald"] = ensure_journald_limit(journald_max_use, dry_run=dry_run, runner=runner)
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["journald"] = {"status": "failed", "error": str(exc)}
    else:
        result["journald"] = {"status": "skipped", "reason": "disabled" if system == "linux" else "not a Linux host"}
    images = result.get("images") if isinstance(result.get("images"), dict) else {}
    cache = result.get("buildCache") if isinstance(result.get("buildCache"), dict) else {}
    result["reclaimedBytes"] = int(images.get("reclaimedBytes") or 0) + int(cache.get("reclaimedBytes") or 0)
    result["finishedAt"] = int(time.time())
    phase_statuses = [str(item.get("status") or "") for item in (images, cache, result["journald"]) if isinstance(item, dict)]
    if "failed" in phase_statuses:
        result["status"] = "failed"
    elif "partial" in phase_statuses or images.get("status") == "skipped":
        # Skipped image cleanup means a safety check stopped it; surface that.
        result["status"] = "partial"
    else:
        result["status"] = "ok"
    result["message"] = summarize(result)
    return result


def summarize(result: Mapping[str, Any]) -> str:
    images = result.get("images") if isinstance(result.get("images"), Mapping) else {}
    count = int(images.get("candidateCount") or 0)
    if result.get("dryRun"):
        if images.get("status") != "ok":
            text = f"Image cleanup would be skipped: {images.get('reason') or images.get('error') or images.get('status')}"
        else:
            text = f"Would remove {count} unused image(s)"
            if count and "uniqueBytes" in images:
                text += f", freeing at least {format_bytes(int(images.get('uniqueBytes') or 0))}"
            if count:
                text += f" (image sizes total {format_bytes(int(images.get('estimatedBytes') or 0))})"
        return text
    if images.get("status") in {"ok", "partial", "failed"} and "removedCount" in images:
        text = f"Removed {int(images.get('removedCount') or 0)} unused image(s)"
        failed = len(images.get("failed") or [])
        if failed:
            text += f", {failed} failed"
    elif images.get("status") == "ok":
        text = "No unused image to remove"
    else:
        text = f"Image cleanup skipped: {images.get('reason') or images.get('error') or images.get('status')}"
    return f"{text}; reclaimed {format_bytes(int(result.get('reclaimedBytes') or 0))}"


def status_path_for(config_path: Path) -> Path:
    return config_path.parent / STATUS_FILE_NAME


def load_status(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_status(path: Path, status: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(dict(status), separators=(",", ":"), sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def update_first_seen(status: Mapping[str, Any], image_ids: Iterable[str], *, now: float) -> Dict[str, float]:
    """Remember when each present image was first observed; forget removed ones.

    The first scan records ``0`` for every image already on the host: those
    keep being judged by their build time, so tracking does not postpone the
    first cleanup. Only images that appear afterwards get a real first-seen
    time.
    """
    raw = status.get("imagesFirstSeen")
    baseline = not isinstance(raw, Mapping)
    previous: Mapping[str, Any] = {} if baseline else raw
    current: Dict[str, float] = {}
    for image_id in image_ids:
        if baseline:
            current[image_id] = 0.0
            continue
        if image_id not in previous:
            current[image_id] = float(int(now))
            continue
        try:
            current[image_id] = max(float(previous.get(image_id) or 0), 0.0)
        except (TypeError, ValueError):
            current[image_id] = float(int(now))
    return current


def record_run(status: Mapping[str, Any], result: Mapping[str, Any], *, trigger: str) -> Dict[str, Any]:
    images = result.get("images") if isinstance(result.get("images"), Mapping) else {}
    updated = dict(status)
    updated.update(
        {
            "lastRunAt": int(result.get("finishedAt") or time.time()),
            "lastTrigger": trigger,
            "lastStatus": str(result.get("status") or ""),
            "lastMessage": str(result.get("message") or "")[:500],
            "lastReclaimedBytes": int(result.get("reclaimedBytes") or 0),
            "lastRemovedImages": int(images.get("removedCount") or 0),
            "lastFailedImages": len(images.get("failed") or []),
        }
    )
    return updated


def heartbeat_summary(status: Mapping[str, Any], settings: DiskHygieneSettings) -> Dict[str, Any]:
    """Compact, Control-safe view of the last cleanup for agent diagnostics."""
    summary: Dict[str, Any] = {
        "enabled": settings.enabled,
        "intervalHours": settings.interval_hours,
        "minAgeDays": settings.min_age_days,
        "journaldMaxUse": settings.journald_max_use,
    }
    last_run = int(status.get("lastRunAt") or 0)
    if last_run:
        summary.update(
            {
                "lastRunAt": last_run,
                "lastTrigger": str(status.get("lastTrigger") or ""),
                "lastStatus": str(status.get("lastStatus") or ""),
                "lastMessage": str(status.get("lastMessage") or "")[:500],
                "lastReclaimedBytes": int(status.get("lastReclaimedBytes") or 0),
                "lastRemovedImages": int(status.get("lastRemovedImages") or 0),
                "lastFailedImages": int(status.get("lastFailedImages") or 0),
            }
        )
    if settings.enabled:
        anchor = max(last_run, int(status.get("lastAttemptAt") or 0))
        summary["nextRunAt"] = anchor + settings.interval_hours * 3600 if anchor else 0
    return summary


def is_due(status: Mapping[str, Any], settings: DiskHygieneSettings, *, now: float) -> bool:
    if not settings.enabled:
        return False
    anchor = max(int(status.get("lastRunAt") or 0), int(status.get("lastAttemptAt") or 0))
    return now - anchor >= settings.interval_hours * 3600


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Report (or apply) Luma node disk hygiene.")
    parser.add_argument("--apply", action="store_true", help="remove the selected images and cache (default: dry run)")
    parser.add_argument("--min-age-days", type=int, default=DEFAULT_MIN_AGE_DAYS)
    parser.add_argument("--control-tag", action="append", default=[], help="Luma Control tag to keep, for example v0.2.18 (repeatable)")
    parser.add_argument("--protect", action="append", default=[], help="image reference to keep (repeatable)")
    parser.add_argument("--no-journald", action="store_true")
    args = parser.parse_args(argv)
    result = run_disk_hygiene(
        min_age_days=max(1, args.min_age_days),
        dry_run=not args.apply,
        journald_max_use="" if args.no_journald else DEFAULT_JOURNALD_MAX_USE,
        protected_refs=args.protect,
        control_tags=args.control_tag,
        buildx_config_root=None,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
