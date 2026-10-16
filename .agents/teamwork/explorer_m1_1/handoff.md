# Handoff Report: M1-1 Native Build Capabilities & Platform Validation Architecture

**Author**: Explorer M1-1  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/explorer_m1_1`  
**Target Milestone**: M1 (Build & Native Architecture)  
**Target Module**: `luma/build_capabilities.py` (and integration with `luma/paas/build/controller.py`)

---

## 1. Observation

### 1.1 Existing `luma/build_capabilities.py` Implementation
In `luma/build_capabilities.py` (lines 15–36):
```python
def native_arm_build_platform(platforms: Iterable[str]) -> str:
    """Return the ARM engine a build requires, never an emulated substitute."""
    required = set()
    for value in platforms:
        for platform in str(value).lower().split(","):
            parts = platform.strip().split("/")
            if len(parts) < 2:
                continue
            os_name, arch = parts[:2]
            variant = parts[2] if len(parts) > 2 else ""
            if arch in {"arm64", "aarch64"}:
                if variant not in {"", "v8"}:
                    raise LumaError(f"cannot verify a native builder for {platform}; ARM emulation is disabled")
                required.add(f"{os_name}/arm64")
            elif arch.startswith("arm"):
                variant = variant or {"armv6l": "v6", "armv7l": "v7", "armhf": "v7", "arm": "v7"}.get(arch)
                if not variant:
                    raise LumaError(f"cannot verify a native builder for {platform}; ARM emulation is disabled")
                required.add(f"{os_name}/arm/{variant}")
    if len(required) > 1:
        raise LumaError("builds targeting different ARM architectures must be submitted separately; ARM emulation is disabled")
    return next(iter(required), "")
```

### 1.2 Existing Usages of `native_arm_build_platform`
A repository-wide search (`grep_search` across `infra-stacks`) identified 5 key call sites:
1. `tests/test_native_builders.py:143-148`:
   ```python
   self.assertEqual(native_arm_build_platform(["linux/amd64", "linux/arm64/v8"]), "linux/arm64")
   self.assertEqual(native_arm_build_platform(["linux/arm/v7"]), "linux/arm/v7")
   with self.assertRaisesRegex(LumaError, "must be submitted separately"):
       native_arm_build_platform(["linux/arm/v7", "linux/arm64"])
   ```
   **Direct Observation**: Existing unit tests explicitly test and rely on `native_arm_build_platform(["linux/amd64", "linux/arm64/v8"])` returning `"linux/arm64"`. It filters an input list for ARM platforms rather than rejecting multi-platform inputs.
2. `luma/control/server.py:782, 787, 3691, 3732, 4064, 4069, 4102`:
   Legacy Nomad build scheduling uses `native_arm_build_platform` to determine if a build job requires an ARM builder node.
3. `luma/agent.py:3619, 3843, 3859`:
   Legacy Nomad node agents verify that the Docker daemon architecture matches `native_arm_build_platform` without QEMU emulation.
4. `luma/paas/build/controller.py:168-172`:
   ```python
   def select_native_builder(self, platform: str, *, preferred_node: str = "") -> BuilderEndpoint:
       norm_platform = str(platform or "linux/amd64").strip().lower()
       arm_platform = native_arm_build_platform([norm_platform])
       target_platform = arm_platform or norm_platform
   ```
   **Architectural Disconnect**: In `BuildController`, calling `native_arm_build_platform([norm_platform])` allows a user to supply `"linux/amd64,linux/arm64"`. `native_arm_build_platform` extracts `"linux/arm64"`, silently ignoring `"linux/amd64"`, rather than rejecting multi-platform builds as required by R2.

### 1.3 Region and Topology Configuration
In `luma/regions.py:31-37`:
```python
    "home": {
        "name": "home",
        "builtin": True,
        "egress": EGRESS_PROXY,
        "exposures": ["none", "tailscale-relay", "cloudflare-tunnel", "tcp-relay"],
    },
```
And in `tests/test_control_deploy_guards.py:219`:
```python
{"nodes": {"macmini": {"name": "macmini", "region": "home", "platform": "darwin/arm64"}}}
```
And in `tests/test_paas_engine.py:38-42`:
```python
BuilderEndpoint(
    name="macbook-pro",
    address="tcp://100.64.0.12:1234",
    platform="linux/arm64",
    os_name="darwin",
)
```
**Direct Observation**: Hardware in the `home` region consists of Apple Silicon nodes (`macbook-pro`, `air3`, `macmini`). While the host OS is macOS (`darwin/arm64`), any container builder or runtime executed on these nodes runs Linux containers (`linux/arm64`).

---

## 2. Logic Chain

1. **Step 1: Need for Strict Single-Architecture Validation (`validate_native_platform`)**:
   - *Premise*: Requirement R2 and `PROJECT.md` require zero cross-compilation and zero QEMU emulation. Multi-platform fat manifests require QEMU emulation or multiple builders simultaneously, which causes cache thrashing and slow builds.
   - *Observation*: `native_arm_build_platform` allows comma-separated multi-platform inputs (`"linux/amd64,linux/arm64"`).
   - *Inference*: A dedicated validation function `validate_native_platform(platform: str) -> str` must be introduced. If `","` is present, it must immediately raise:
     `LumaError("cross-architecture emulation and multi-platform compilation are disabled; submit builds separately for each native architecture")`.

2. **Step 2: Architecture Alias Normalization**:
   - *Premise*: Toolchains and Docker clients submit diverse architecture alias representations (`arm64/v8`, `aarch64`, `x86_64`).
   - *Observation*: BuildKit endpoints and K8s node labels (`kubernetes.io/arch`) standardize on `arm64` and `amd64`.
   - *Inference*: Normalization must map:
     - `arm64/v8`, `aarch64` -> `arm64`
     - `x86_64` -> `amd64`
   - *Target Set*: Allowed native targets must strictly be `linux/arm64` or `linux/amd64`, while permitting `darwin/arm64` and `darwin/amd64` for local Darwin evaluation. All other platforms (e.g. `linux/arm/v7`, `windows/amd64`, `linux/mips`) must raise `LumaError`.

3. **Step 3: Topology-Based Native Architecture Inference (`infer_native_platform`)**:
   - *Premise*: Service developers frequently omit `--platform` when submitting builds or deploying services.
   - *Inference*: An inference hierarchy must resolve the platform deterministically:
     1. If `platform` is provided and non-empty: validate via `validate_native_platform(platform)`.
     2. If `platform` is empty:
        - If `target_region == "home"` -> return `"linux/arm64"`.
        - If `target_node in node_architectures` -> return normalized architecture (mapping `darwin/` host architectures to `linux/` container platform).
        - If `target_node in {"macbook-pro", "air3"}` or `"arm64"` in `target_node.lower()` (or `"aarch64"`) -> return `"linux/arm64"`.
        - Otherwise -> default to `"linux/amd64"`.

4. **Step 4: Preserving Backwards Compatibility for `native_arm_build_platform`**:
   - *Premise*: Modifying `native_arm_build_platform` to reject commas or non-ARM strings would break `test_multiple_arm_architectures_cannot_fall_back_to_emulation` in `tests/test_native_builders.py:144` and Nomad control scheduling in `luma/control/server.py`.
   - *Inference*: `native_arm_build_platform` must be retained without breaking changes. `validate_native_platform` and `infer_native_platform` are added alongside it, and PaaS `BuildController` is updated to consume `infer_native_platform`.

---

## 3. Caveats

1. **Host Architecture vs Container Runtime Architecture**:
   - Apple Silicon macOS nodes (e.g. `macbook-pro`, `air3`, `macmini` in the `home` region) report host platform `darwin/arm64`.
   - However, K8s Pods and BuildKit container builds on these nodes run inside Linux VMs (OrbStack / Docker Desktop), so the target container image platform is always `linux/arm64`.
   - `validate_native_platform` allows `darwin/arm64` and `darwin/amd64` for local host evaluation tasks, but `infer_native_platform` maps node architectures to `linux/arm64` and `linux/amd64` for container build targets.
2. **Read-Only Investigation Constraint**:
   - In accordance with the explorer role, no code files in `luma/` or `tests/` were modified during this investigation. The proposed implementations below are ready for direct inclusion by Milestone 1 implementation agents.

---

## 4. Conclusion & Proposed Code Implementation

### 4.1 Exact Code Additions for `luma/build_capabilities.py`

```python
# To be added to luma/build_capabilities.py

from typing import Any, Iterable, Mapping  # ensure Mapping is imported

VALID_NATIVE_PLATFORMS: frozenset[str] = frozenset({
    "linux/arm64",
    "linux/amd64",
    "darwin/arm64",
    "darwin/amd64",
})


def validate_native_platform(platform: str) -> str:
    """Validate and normalize a native single-architecture build platform.

    Enforces strict native building:
    - Multiple platforms or comma-separated lists are rejected with LumaError.
    - Architecture aliases (arm64/v8, aarch64 -> arm64; x86_64 -> amd64) are normalized.
    - Target must resolve to either linux/arm64, linux/amd64, or valid darwin/arm64 / darwin/amd64.
    """
    if platform is None:
        raise LumaError("native build platform is required")
    raw = str(platform).strip()
    if "," in raw:
        raise LumaError(
            "cross-architecture emulation and multi-platform compilation are disabled; "
            "submit builds separately for each native architecture"
        )
    if not raw:
        raise LumaError("native build platform is required")

    lowered = raw.lower()
    parts = [p.strip() for p in lowered.split("/") if p.strip()]

    if len(parts) == 1:
        os_name = "linux"
        arch = parts[0]
        variant = ""
    elif len(parts) == 2:
        if parts[0] in {"linux", "darwin"}:
            os_name = parts[0]
            arch = parts[1]
            variant = ""
        elif parts[0] in {"arm64", "aarch64"} and parts[1] in {"v8", ""}:
            os_name = "linux"
            arch = parts[0]
            variant = parts[1]
        else:
            os_name = parts[0]
            arch = parts[1]
            variant = ""
    elif len(parts) == 3:
        os_name, arch, variant = parts[0], parts[1], parts[2]
    else:
        raise LumaError(f"invalid platform format: {platform!r}")

    if arch in {"arm64", "aarch64"}:
        if variant not in {"", "v8"}:
            raise LumaError(
                f"unsupported architecture variant {variant!r} for {platform}; "
                "only ARM64 (v8) is supported"
            )
        norm_arch = "arm64"
    elif arch in {"amd64", "x86_64"}:
        if variant:
            raise LumaError(
                f"unsupported architecture variant {variant!r} for {platform}; "
                "AMD64 does not accept variants"
            )
        norm_arch = "amd64"
    else:
        raise LumaError(
            f"unsupported native architecture {arch!r} in {platform!r}; "
            "only arm64 and amd64 architectures are supported"
        )

    norm_platform = f"{os_name}/{norm_arch}"
    if norm_platform not in VALID_NATIVE_PLATFORMS:
        raise LumaError(
            f"unsupported native platform: {platform!r}; "
            "supported platforms are linux/arm64, linux/amd64 (or darwin/arm64, darwin/amd64)"
        )
    return norm_platform


def infer_native_platform(
    *,
    platform: str = "",
    target_node: str = "",
    target_region: str = "",
    node_architectures: Mapping[str, str] | None = None,
) -> str:
    """Infer the native target build platform based on deployment topology.

    Inference priority:
    1. If `platform` is provided and non-empty: validate and normalize via `validate_native_platform`.
    2. If `platform` is empty:
       - If `target_region == "home"` -> return "linux/arm64".
       - If `target_node` in `node_architectures` -> return normalized architecture.
       - If `target_node in {"macbook-pro", "air3"}` or `"arm64"` in `target_node.lower()` -> return "linux/arm64".
       - Otherwise default to "linux/amd64".
    """
    clean_platform = str(platform or "").strip()
    if clean_platform:
        return validate_native_platform(clean_platform)

    clean_region = str(target_region or "").strip().lower()
    if clean_region == "home":
        return "linux/arm64"

    clean_node = str(target_node or "").strip()
    if clean_node and node_architectures:
        node_arch = node_architectures.get(clean_node)
        if not node_arch and clean_node.lower() in node_architectures:
            node_arch = node_architectures[clean_node.lower()]
        if node_arch:
            norm = validate_native_platform(str(node_arch).strip())
            if norm.startswith("darwin/"):
                return f"linux/{norm.partition('/')[2]}"
            return norm

    lower_node = clean_node.lower()
    if lower_node in {"macbook-pro", "air3"} or "arm64" in lower_node or "aarch64" in lower_node:
        return "linux/arm64"

    return "linux/amd64"
```

### 4.2 Proposed Integration in `luma/paas/build/controller.py`

In `luma/paas/build/controller.py`:
```python
# Import infer_native_platform and validate_native_platform
from ...build_capabilities import infer_native_platform, native_arm_build_platform, validate_native_platform

# In BuildController.select_native_builder:
def select_native_builder(
    self,
    platform: str = "",
    *,
    preferred_node: str = "",
    target_node: str = "",
    target_region: str = "",
) -> BuilderEndpoint:
    """Select a ready native BuilderEndpoint for the target platform (never QEMU)."""
    target_platform = infer_native_platform(
        platform=platform,
        target_node=preferred_node or target_node,
        target_region=target_region,
    )
    ...
```

---

## 5. Verification Method

### 5.1 Automated Test Execution Command
Run the test suites using the project virtual environment:
```bash
PATH="$PWD/.venv/bin:$PATH" ./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v
```

### 5.2 Unit Test Assertions Specification
Add the following test cases to `tests/test_paas_engine.py`:

```python
class NativePlatformValidationTests(unittest.TestCase):
    def test_validate_native_platform_canonical_and_aliases(self) -> None:
        self.assertEqual(validate_native_platform("linux/arm64"), "linux/arm64")
        self.assertEqual(validate_native_platform("linux/amd64"), "linux/amd64")
        self.assertEqual(validate_native_platform("linux/arm64/v8"), "linux/arm64")
        self.assertEqual(validate_native_platform("linux/aarch64"), "linux/arm64")
        self.assertEqual(validate_native_platform("linux/x86_64"), "linux/amd64")
        self.assertEqual(validate_native_platform("arm64"), "linux/arm64")
        self.assertEqual(validate_native_platform("arm64/v8"), "linux/arm64")
        self.assertEqual(validate_native_platform("aarch64"), "linux/arm64")
        self.assertEqual(validate_native_platform("amd64"), "linux/amd64")
        self.assertEqual(validate_native_platform("x86_64"), "linux/amd64")
        self.assertEqual(validate_native_platform("darwin/arm64"), "darwin/arm64")
        self.assertEqual(validate_native_platform("darwin/x86_64"), "darwin/amd64")

    def test_validate_native_platform_rejects_multi_platform(self) -> None:
        err = "cross-architecture emulation and multi-platform compilation are disabled; submit builds separately for each native architecture"
        with self.assertRaisesRegex(LumaError, err):
            validate_native_platform("linux/amd64,linux/arm64")
        with self.assertRaisesRegex(LumaError, err):
            validate_native_platform("linux/arm64,linux/amd64")
        with self.assertRaisesRegex(LumaError, err):
            validate_native_platform("linux/amd64, linux/arm64")
        with self.assertRaisesRegex(LumaError, err):
            validate_native_platform("linux/arm64,")

    def test_validate_native_platform_rejects_unsupported(self) -> None:
        with self.assertRaises(LumaError):
            validate_native_platform("")
        with self.assertRaises(LumaError):
            validate_native_platform("linux/arm/v7")
        with self.assertRaises(LumaError):
            validate_native_platform("windows/amd64")
        with self.assertRaises(LumaError):
            validate_native_platform("linux/arm64/v7")

    def test_infer_native_platform_rules(self) -> None:
        # Rule 1: Explicit platform overrides all
        self.assertEqual(
            infer_native_platform(platform="linux/arm64", target_region="cn"),
            "linux/arm64",
        )
        self.assertEqual(
            infer_native_platform(platform="linux/amd64", target_region="home"),
            "linux/amd64",
        )

        # Rule 2: Empty platform + home region -> linux/arm64
        self.assertEqual(infer_native_platform(target_region="home"), "linux/arm64")

        # Rule 3: Node architectures mapping
        nodes = {
            "microos-arm-01": "linux/arm64",
            "microos-amd-01": "linux/amd64",
            "macmini": "darwin/arm64",
        }
        self.assertEqual(
            infer_native_platform(target_node="microos-arm-01", node_architectures=nodes),
            "linux/arm64",
        )
        self.assertEqual(
            infer_native_platform(target_node="microos-amd-01", node_architectures=nodes),
            "linux/amd64",
        )
        self.assertEqual(
            infer_native_platform(target_node="macmini", node_architectures=nodes),
            "linux/arm64",
        )

        # Rule 4: Node name heuristics
        self.assertEqual(infer_native_platform(target_node="macbook-pro"), "linux/arm64")
        self.assertEqual(infer_native_platform(target_node="air3"), "linux/arm64")
        self.assertEqual(infer_native_platform(target_node="opensuse-microos-arm64"), "linux/arm64")

        # Rule 5: Default fallback
        self.assertEqual(infer_native_platform(), "linux/amd64")
        self.assertEqual(infer_native_platform(target_region="cn"), "linux/amd64")
```

### 5.3 Invalidation Conditions
This design is invalidated if:
1. Docker Buildx multi-arch fat manifest compilation is re-enabled in Luma PaaS (violating Requirement R2).
2. Existing tests in `test_native_builders.py` for `native_arm_build_platform` fail.
