# Handoff Report: Feature 3 — Zot Retention Pins & Obsolete Tag Pruning

**Investigator**: Explorer M2-1  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/explorer_m2_1`  
**Target Milestone**: Milestone 2 (Feature 3: Zot Retention Pins & Obsolete Tag Pruning)  
**Primary Files**:
- `luma/paas/build/artifact_store.py`
- `deploy/paas-stack/zot/zot-config.json`
- `luma/paas/release/flux_reconciler.py`
- `luma/registry_management.py`
- `tests/test_paas_engine.py`

---

## 1. Observation

### Observation 1.1: Retention Tag Naming Disconnect
In `luma/paas/build/artifact_store.py` (lines 313–339):
```python
313:     for (project, env), target_releases in by_target.items():
314:         ordered = sorted(target_releases, key=lambda r: (r.generation, r.created_at), reverse=True)
315:         active = next((r for r in ordered if r.status in {"healthy", "reconciling", "probing"}), None)
316:         if active and active.artifact_id in artifacts:
317:             art = artifacts[active.artifact_id]
318:             for img in art.images.values():
319:                 _mark(
320:                     img,
321:                     project=project,
322:                     environment=env,
323:                     release_id=active.release_id,
324:                     status="active",
325:                     pin_tag=f"luma-pin-{project}-{env}-active",
326:                 )
327:         healthy_history = [r for r in ordered if r.status in {"healthy", "rolled_back"}][:rollback_window]
328:         for idx, rel in enumerate(healthy_history, start=1):
329:             art = artifacts.get(rel.artifact_id)
330:             if not art:
331:                 continue
332:             for img in art.images.values():
333:                 _mark(
334:                     img,
335:                     project=project,
336:                     environment=env,
337:                     release_id=rel.release_id,
338:                     status="rollback",
339:                     pin_tag=f"luma-pin-{project}-{env}-rb{idx}",
340:                 )
```
- Line 325 emits `f"luma-pin-{project}-{env}-active"` (e.g. `luma-pin-serverx-prod-active`).
- Line 339 emits `f"luma-pin-{project}-{env}-rb{idx}"` (e.g. `luma-pin-serverx-prod-rb1`).
- Line 327 includes `active` in `healthy_history`, causing `healthy_history` to consume a rollback slot for the active release, meaning only $N-1$ true rollback releases are retained instead of $N=5$.
- In contrast, the contract established in:
  - `docs/build-and-artifacts.md` (line 31): `luma-pin-active-<project>-<env>` and `luma-pin-rollback-<project>-<env>-v<gen>`
  - `docs/build-and-artifacts.zh-CN.md` (line 31): `luma-pin-active-<project>-<env>` and `luma-pin-rollback-<project>-<env>-v<gen>`
  - `dashboard-src/src/components/ReleaseTimelinePanel.tsx` (lines 214–215):
    ```typescript
    ? `luma-pin-active-${stack}-prod`
    : `luma-pin-rollback-${stack}-prod-v${currentNum}`
    ```
  - `orchestrator/PROJECT.md` (lines 17, 43): `luma-pin-active-*`, `luma-pin-rollback-*`
  - `TEST_INFRA.md` (lines 43, 72–73, 412–413):
    ```python
    active_tag = f"luma-pin-active-granary-prod"
    rb_tag = f"luma-pin-rollback-granary-prod-v1"
    pins = self.catalog.protected_pin_tags("granary", "prod", rollback_window=5)
    self.assertIn(active_tag, pins)
    ```

### Observation 1.2: Complete Absence of Obsolete Pin Pruning & Missing Methods
In `luma/paas/build/artifact_store.py`:
- `ArtifactCatalog` (lines 354–412) has **no** `sync_retention_pins` method and **no** `protected_pin_tags` method.
- `ZotRegistryClient` (lines 414–504) contains only `pin_digest`, `sync_retention_pins` (which only performs `PUT` without tag discovery or pruning), and `inspect_image_layers`. It has **no** `delete_tag` method and **no** `list_tags` method.
- In `deploy/paas-stack/zot/zot-config.json` (lines 9–31):
  ```json
  "retention": {
    "dryRun": false,
    "policies": [
      {
        "repositories": ["**"],
        "deleteReferrers": true,
        "deleteUntagged": true,
        "keepTags": [
          {
            "patterns": ["^luma-pin-.*", "^release-.*"]
          },
  ```
  Zot's retention policy preserves any tag matching `^luma-pin-.*`. Without explicit tag pruning via the OCI manifest/tag delete API, tags outside the rollback window remain on the registry indefinitely, completely preventing Zot's online GC from reclaiming disk space for dead release layers.
- In `TEST_INFRA.md` (lines 449–453):
  ```python
  pruned = self.catalog.sync_retention_pins(releases, rollback_window=5)
  # 8 releases -> 1 active + 5 rollback = 6 kept, 2 pruned
  self.assertEqual(len(pruned["deleted_tags"]), 2)
  self.assertEqual(len(pruned["retained_tags"]), 6)
  ```
  Currently calling `self.catalog.sync_retention_pins(...)` raises `AttributeError: 'ArtifactCatalog' object has no attribute 'sync_retention_pins'`.

### Observation 1.3: Registry Repository Validation Crash on Host:Port URLs
In `luma/registry_management.py` (lines 40–47):
```python
_REPOSITORY_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")

def validate_repository(value: Any) -> str:
    repository = str(value or "").strip().lower().strip("/")
    if not repository or not _REPOSITORY_RE.fullmatch(repository):
        raise LumaError(f"invalid registry repository: {value}")
    return repository
```
- `_REPOSITORY_RE` rejects colons `:`.
- In `luma/paas/build/artifact_store.py` (line 436):
  ```python
  def pin_digest(self, repository: str, digest: str, pin_tag: str) -> None:
      """Retag a manifest digest in-place via OCI Manifest PUT without pulling blobs."""
      repository = validate_repository(repository)
  ```
  Calling `pin_digest("100.66.177.70:5000/serverx-web", digest, tag)` or `"registry.internal:5050/subpath/app"` directly fails with `LumaError: invalid registry repository: 100.66.177.70:5000/serverx-web` because `:5000` does not match `_REPOSITORY_RE`.
- Note that `ServiceImageArtifact.__post_init__` (lines 67–72) already realized this issue and stripped host:port locally, but `ZotRegistryClient.pin_digest` was left unpatched:
  ```python
  repo_path = self.repository
  if "/" in repo_path:
      first_seg, rest = repo_path.split("/", 1)
      if ":" in first_seg or "." in first_seg or first_seg == "localhost":
          repo_path = rest
  validate_repository(repo_path)
  ```
- Furthermore, `ArtifactCatalog` lacks `normalize_repository(repository)` which is explicitly tested in `TEST_INFRA.md` (line 421):
  ```python
  norm = self.catalog.normalize_repository("100.66.177.70:5000/serverx-web")
  self.assertEqual(norm, "serverx-web")
  ```

### Observation 1.4: Multi-Service Compose Stacks Sharing Digests Drop Repositories
In `luma/paas/build/artifact_store.py` (lines 283–310):
```python
    protected: Dict[str, Dict[str, Any]] = {}

    def _mark(
        img: ServiceImageArtifact,
        *,
        project: str,
        environment: str,
        release_id: str,
        status: str,
        pin_tag: str,
    ) -> None:
        entry = protected.setdefault(
            img.digest,
            {
                "digest": img.digest,
                "repository": img.repository,
                "status": status,
                "projects": set(),
                "environments": set(),
                "releases": set(),
                "pinTags": set(),
            },
        )
```
- In multi-service Compose applications, if two services (`web` in repository `100.66.177.70:5000/serverx-web` and `worker` in repository `100.66.177.70:5000/serverx-worker`) share the same base image digest (e.g. `sha256:abc...`), `protected.setdefault(img.digest, ...)` executes only once for `img.digest`.
- As a consequence, `entry["repository"]` retains only the first repository (`serverx-web`). The repository for `worker` (`serverx-worker`) is completely discarded!
- When `ZotRegistryClient.sync_retention_pins` (lines 457–463) runs:
  ```python
  for digest, info in protected.items():
      repo = str(info.get("repository") or "")
      for tag in info.get("pinTags") or []:
          self.pin_digest(repo, digest, str(tag))
  ```
  It pins only `serverx-web`. `serverx-worker` receives zero pin tags in Zot. Zot online GC then detects `serverx-worker` as unpinned and purges its manifest, leaving the worker workload broken on subsequent restarts or rollbacks!
- Furthermore, if 3 services share the exact same repository and digest, there is no deduplication guard across `(repository, digest, pin_tag)`, leading to redundant OCI `PUT` requests (violating TC-B-F3-05).

---

## 2. Logic Chain

1. **Tag Naming Contract**:
   - `docs/build-and-artifacts.md` and `dashboard-src/` display pins as `luma-pin-active-<project>-<env>` and `luma-pin-rollback-<project>-<env>-v<gen>`.
   - By matching the contract, operator tooling, dashboard cards, and release timeline diffs have uniform visibility over active pins vs rollback pins.
   - Using the release generation `v{rel.generation}` ensures each tag is immutable, monotonic, and idempotent, unlike relative positional indexes (`rb1`, `rb2`) which shift across releases.
   - For a rollback window of $N=5$, the active release is 1 slot, and prior healthy releases provide up to 5 rollback slots (total 6 releases protected). Excluding `active` from `healthy_history` guarantees that exactly 5 historical releases are retained.

2. **Obsolete Pin Pruning Execution**:
   - Zot Registry evaluates `retention.policies.keepTags` with regex `^luma-pin-.*`.
   - Any tag in Zot matching `^luma-pin-.*` is strictly preserved from GC.
   - Therefore, to permit Zot online GC to reclaim orphaned layers, Luma must proactively delete obsolete tags when releases fall out of the active + rollback window ($N=5$).
   - In OCI Distribution Spec / Zot Registry API, tag deletion is executed via:
     `DELETE /v2/<repository>/manifests/<tag>`
     returning HTTP 200, 202, 204, or 404 (if already deleted).
   - In `ArtifactCatalog.sync_retention_pins(releases, rollback_window=5)`:
     - Active tag: `luma-pin-active-{project}-{env}`
     - Retained rollback tags: `luma-pin-rollback-{project}-{env}-v{r.generation}` for the top $N$ healthy previous releases.
     - Pruned obsolete tags: any `luma-pin-rollback-{project}-{env}-v{r.generation}` (or legacy `luma-pin-{project}-{env}-*`) belonging to the project and environment outside this window.
     - The return type must satisfy both sequence iteration (`Sequence[str]` of deleted tags as specified in `PROJECT.md`) and dictionary lookups (`pruned["deleted_tags"]` and `pruned["retained_tags"]` as required by `TEST_INFRA.md`). A lightweight `RetentionSyncResult(list)` accomplishes both without conflict.

3. **Registry Repository Normalization**:
   - Container image references in Compose, Dockerfiles, and Helm templates frequently contain the registry host and port (e.g. `100.66.177.70:5000/serverx-web` or `registry.internal:5050/subpath/app`).
   - OCI Distribution Specification repository paths are relative to the registry root (e.g. `/v2/serverx-web/manifests/...`).
   - `ZotRegistryClient.http` is initialized with `base_url="http://100.66.177.70:5000"`.
   - Stripping the host/port before validation and URL path construction prevents `_REPOSITORY_RE` syntax rejection and ensures correct OCI endpoint routing.
   - Exposing `normalize_repository(repository)` as both a standalone function and an instance method on `ArtifactCatalog` and `ZotRegistryClient` fulfills test contracts directly.

4. **Multi-Service Independent Repository Pinning**:
   - In microservice architectures, different services (`web`, `worker`, `agent`) push to separate OCI repositories (`.../serverx-web`, `.../serverx-worker`).
   - Because Zot partitions manifests and tags per repository path, a tag created in `serverx-web` does not exist in `serverx-worker`.
   - `compute_protected_digests` must track repositories per digest (`repositories: set[str]` and `repositoryPinTags: dict[str, set[str]]`).
   - `ZotRegistryClient.sync_retention_pins` must iterate across all distinct repositories for each digest, maintaining a `seen_pins = set()` of `(repository, digest, tag)` tuples to guarantee both multi-repository coverage and duplicate-call deduplication.

---

## 3. Caveats

- **Zot Offline vs Online GC**: Zot configuration in `deploy/paas-stack/zot/zot-config.json` uses `"gc": true`, `"deleteUntagged": true`, and `"gcDelay": "1h"`. Deleted tags remove manifest references immediately, but blob deletion on disk occurs asynchronously during Zot's periodic GC cycle (1 hour delay). Automated test assertions must verify tag deletion via the registry client API rather than assuming instantaneous filesystem blob unlinking.
- **Legacy Tag Format Cleanup**: Registries may contain tags created under the old naming format (`luma-pin-<proj>-<env>-active` or `...-rb<idx>`). The pruning regex in `sync_retention_pins` should recognize both new and legacy formats so existing clusters self-heal during reconciliation.
- **No Caveats Beyond Scope**: The interface contracts between `ArtifactCatalog`, `ZotRegistryClient`, and `FluxReconciler` are clean and self-contained.

---

## 4. Conclusion & Concrete Remediation Plan

To fulfill Milestone 2 Feature 3 completely, the following exact code changes must be applied to `luma/paas/build/artifact_store.py`:

### Remediation 4.1: Repository Normalization Function & Method
Add `normalize_repository(value: Any) -> str` at module level and wire to `ArtifactCatalog` and `ZotRegistryClient`:
```python
def normalize_repository(value: Any) -> str:
    """Strip registry host and port (e.g. '100.66.177.70:5000/app' -> 'app') and validate OCI repository path."""
    raw = str(value or "").strip()
    if "/" in raw:
        first_seg, rest = raw.split("/", 1)
        if ":" in first_seg or "." in first_seg or first_seg.lower() == "localhost":
            raw = rest
    return validate_repository(raw)
```
In `ArtifactCatalog`:
```python
    def normalize_repository(self, repository: str) -> str:
        return normalize_repository(repository)
```

### Remediation 4.2: Retention Tag Naming & Multi-Repository Digest Tracking
In `compute_protected_digests` (`artifact_store.py` lines 263–351):
```python
def compute_protected_digests(
    artifacts: Mapping[str, BuildArtifact],
    releases: Iterable[ReleaseRecord],
    *,
    rollback_window: int = 5,
) -> Dict[str, Dict[str, Any]]:
    rollback_window = max(int(rollback_window), 1)
    by_target: Dict[tuple[str, str], list[ReleaseRecord]] = {}
    for rel in releases:
        if rel.status in {"healthy", "reconciling", "probing", "rolled_back"}:
            by_target.setdefault((rel.project, rel.environment), []).append(rel)

    protected: Dict[str, Dict[str, Any]] = {}

    def _mark(
        img: ServiceImageArtifact,
        *,
        project: str,
        environment: str,
        release_id: str,
        status: str,
        pin_tag: str,
    ) -> None:
        entry = protected.setdefault(
            img.digest,
            {
                "digest": img.digest,
                "repository": img.repository,
                "repositories": set(),
                "status": status,
                "projects": set(),
                "environments": set(),
                "releases": set(),
                "pinTags": set(),
                "repositoryPinTags": {},
            },
        )
        if status == "active":
            entry["status"] = "active"
        entry["repositories"].add(img.repository)
        entry["projects"].add(project)
        entry["environments"].add(environment)
        entry["releases"].add(release_id)
        entry["pinTags"].add(pin_tag)
        entry["repositoryPinTags"].setdefault(img.repository, set()).add(pin_tag)

    for (project, env), target_releases in by_target.items():
        ordered = sorted(target_releases, key=lambda r: (r.generation, r.created_at), reverse=True)
        active = next((r for r in ordered if r.status in {"healthy", "reconciling", "probing"}), None)
        if active and active.artifact_id in artifacts:
            art = artifacts[active.artifact_id]
            for img in art.images.values():
                _mark(
                    img,
                    project=project,
                    environment=env,
                    release_id=active.release_id,
                    status="active",
                    pin_tag=f"luma-pin-active-{project}-{env}",
                )
        healthy_history = [
            r for r in ordered
            if r.status in {"healthy", "rolled_back"} and (active is None or r.release_id != active.release_id)
        ][:rollback_window]
        for rel in healthy_history:
            art = artifacts.get(rel.artifact_id)
            if not art:
                continue
            for img in art.images.values():
                _mark(
                    img,
                    project=project,
                    environment=env,
                    release_id=rel.release_id,
                    status="rollback",
                    pin_tag=f"luma-pin-rollback-{project}-{env}-v{rel.generation}",
                )

    result: Dict[str, Dict[str, Any]] = {}
    for digest, data in protected.items():
        result[digest] = {
            "digest": digest,
            "repository": data["repository"],
            "repositories": sorted(data["repositories"]),
            "status": data["status"],
            "projects": sorted(data["projects"]),
            "environments": sorted(data["environments"]),
            "releases": sorted(data["releases"]),
            "pinTags": sorted(data["pinTags"]),
            "repositoryPinTags": {r: sorted(tags) for r, tags in data["repositoryPinTags"].items()},
        }
    return result
```

### Remediation 4.3: RetentionSyncResult & Obsolete Tag Pruning in ArtifactCatalog
Add `RetentionSyncResult` class and helper methods in `ArtifactCatalog`:
```python
WorkloadRelease = ReleaseRecord  # Contract type alias

class RetentionSyncResult(list):
    """Dual-interface result: acts as a Sequence[str] of deleted tags and as a dict with deleted_tags/retained_tags."""

    def __init__(self, deleted_tags: Iterable[str], retained_tags: Iterable[str]) -> None:
        del_list = sorted(set(deleted_tags))
        ret_list = sorted(set(retained_tags))
        super().__init__(del_list)
        self.deleted_tags = del_list
        self.retained_tags = ret_list

    def __getitem__(self, item: Any) -> Any:
        if item == "deleted_tags":
            return self.deleted_tags
        if item == "retained_tags":
            return self.retained_tags
        return super().__getitem__(item)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "deleted_tags":
            return self.deleted_tags
        if key == "retained_tags":
            return self.retained_tags
        return default
```
In `ArtifactCatalog`:
```python
    def protected_pin_tags(
        self,
        project: str,
        environment: str = "prod",
        *,
        rollback_window: int = 5,
    ) -> list[str]:
        """Return all active and rollback pin tags currently protected for project and environment."""
        digests = self.protected_digests(rollback_window=rollback_window)
        pins: set[str] = set()
        for info in digests.values():
            if project in info.get("projects", []) and environment in info.get("environments", []):
                pins.update(info.get("pinTags", []))
        return sorted(pins)

    def sync_retention_pins(
        self,
        releases: Sequence[ReleaseRecord] | None = None,
        *,
        rollback_window: int = 5,
        zot_client: ZotRegistryClient | None = None,
    ) -> RetentionSyncResult:
        """Synchronize retention pins and prune obsolete tags beyond the rollback window."""
        rollback_window = max(int(rollback_window), 1)
        rel_list = list(releases if releases is not None else self.list_releases())
        if not rel_list:
            return RetentionSyncResult([], [])

        # Group releases by (project, environment)
        by_target: Dict[tuple[str, str], list[ReleaseRecord]] = {}
        for r in rel_list:
            by_target.setdefault((r.project, r.environment), []).append(r)

        all_retained: set[str] = set()
        all_deleted: set[str] = set()

        for (proj, env), target_releases in by_target.items():
            ordered = sorted(target_releases, key=lambda r: (r.generation, r.created_at), reverse=True)
            active = next((r for r in ordered if r.status in {"healthy", "reconciling", "probing"}), None)
            target_retained: set[str] = set()
            if active:
                target_retained.add(f"luma-pin-active-{proj}-{env}")
            healthy_history = [
                r for r in ordered
                if r.status in {"healthy", "rolled_back"} and (active is None or r.release_id != active.release_id)
            ][:rollback_window]
            for r in healthy_history:
                target_retained.add(f"luma-pin-rollback-{proj}-{env}-v{r.generation}")
            all_retained.update(target_retained)

            # Identify candidate obsolete tags from releases outside active + rollback window
            older_releases = [
                r for r in ordered
                if (active is None or r.release_id != active.release_id) and r not in healthy_history
            ]
            for r in older_releases:
                tag = f"luma-pin-rollback-{proj}-{env}-v{r.generation}"
                if tag not in target_retained:
                    all_deleted.add(tag)

            # If Zot client is provided, query existing tags from registry and prune
            if zot_client is not None:
                # Collect repositories for this project's artifacts
                project_artifacts = self.list_artifacts(project=proj)
                repos: set[str] = set()
                for art in project_artifacts:
                    for img in art.images.values():
                        repos.add(img.repository)
                pin_regex = re.compile(
                    rf"^luma-pin-(?:(?:active|rollback)-{re.escape(proj)}-{re.escape(env)}(?:-v\d+)?|{re.escape(proj)}-{re.escape(env)}-.*)$"
                )
                for repo in repos:
                    try:
                        existing_tags = zot_client.list_tags(repo)
                    except LumaError:
                        continue
                    for tag in existing_tags:
                        if pin_regex.fullmatch(tag) and tag not in target_retained:
                            all_deleted.add(tag)
                            try:
                                zot_client.delete_tag(repo, tag)
                            except LumaError:
                                pass

        return RetentionSyncResult(sorted(all_deleted), sorted(all_retained))
```

### Remediation 4.4: ZotRegistryClient delete_tag, list_tags & Deduplicated sync_retention_pins
In `ZotRegistryClient`:
```python
    def normalize_repository(self, repository: str) -> str:
        return normalize_repository(repository)

    def delete_tag(self, repository: str, tag: str) -> None:
        """Delete an obsolete tag reference from Zot OCI Registry so online GC can reclaim blobs."""
        repository = normalize_repository(repository)
        tag = str(tag or "").strip()
        if not tag or any(c in tag for c in ("/", "?", "#", "\0", "\n", "\r")):
            raise LumaError(f"invalid registry tag: {tag}")
        encoded_repo = urllib.parse.quote(repository, safe="/")
        encoded_tag = urllib.parse.quote(tag, safe="")
        self.http.request(
            "DELETE",
            f"/v2/{encoded_repo}/manifests/{encoded_tag}",
            expected=(200, 202, 204, 404),
        )

    def list_tags(self, repository: str) -> list[str]:
        """List all tags in the given repository."""
        return self.http.tags(normalize_repository(repository))

    def pin_digest(self, repository: str, digest: str, pin_tag: str) -> None:
        """Retag a manifest digest in-place via OCI Manifest PUT without pulling blobs."""
        repository = normalize_repository(repository)
        digest = validate_digest(digest)
        _, response = self.http.manifest(repository, digest)
        content_type = (
            response.headers.get("Content-Type")
            or response.headers.get("content-type")
            or "application/vnd.oci.image.manifest.v1+json"
        ).split(";", 1)[0].strip()
        encoded_repo = urllib.parse.quote(repository, safe="/")
        self.http.request(
            "PUT",
            f"/v2/{encoded_repo}/manifests/{urllib.parse.quote(pin_tag, safe='')}",
            content_type=content_type,
            body=response.body,
            expected=(200, 201, 202),
        )

    def sync_retention_pins(
        self,
        protected: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Synchronize retention pin tags across all repositories independently with deduplication."""
        pinned_count = 0
        seen_pins: set[tuple[str, str, str]] = set()  # (repo, digest, tag) deduplication guard
        for digest, info in protected.items():
            repo_pins = info.get("repositoryPinTags")
            if isinstance(repo_pins, Mapping) and repo_pins:
                for repo, tags in repo_pins.items():
                    for tag in tags:
                        key = (str(repo), digest, str(tag))
                        if key not in seen_pins:
                            seen_pins.add(key)
                            self.pin_digest(str(repo), digest, str(tag))
                            pinned_count += 1
            else:
                repos = info.get("repositories") or ([info.get("repository")] if info.get("repository") else [])
                for repo in repos:
                    for tag in info.get("pinTags") or []:
                        key = (str(repo), digest, str(tag))
                        if key not in seen_pins:
                            seen_pins.add(key)
                            self.pin_digest(str(repo), digest, str(tag))
                            pinned_count += 1
        return {
            "protectedDigests": len(protected),
            "appliedPinTags": pinned_count,
            "syncedAt": int(time.time()),
        }
```

---

## 5. Verification Method

1. **Automated Unit Tests**:
   Execute the PaaS engine test suite:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine -v
   ```
   Verify 0 failures and 0 errors.

2. **Milestone 2 Unit Test Verification**:
   The implementer can run the test cases from `TEST_INFRA.md` Suite 2:
   - `test_retention_pin_naming_convention`: asserts `luma-pin-active-granary-prod` and `luma-pin-rollback-granary-prod-v1` are generated.
   - `test_registry_repository_validation_with_port`: asserts `catalog.normalize_repository("100.66.177.70:5000/serverx-web") == "serverx-web"`.
   - `test_obsolete_pin_pruning_beyond_window`: 8 releases deployed; asserts `len(pruned["deleted_tags"]) == 2` and `len(pruned["retained_tags"]) == 6`.
   - Boundary tests TC-B-F3-01 to TC-B-F3-05.

3. **Full Repository Gate**:
   Execute the full gate script:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
   Ensure ruff linting, version checks, dashboard builds, and unit tests pass with exit code `0`.
