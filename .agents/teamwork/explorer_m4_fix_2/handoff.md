# Handoff Report: Explorer M4-Fix-2

## Executive Summary
This report presents the complete investigation, root cause analysis, and production-ready remediation design for **Finding 2** reported by Reviewer M4-1:
The unanchored substring search `h in no_proxy_raw` on line 211 of `luma/paas/migration/migrator.py` causes false-positive proxy bypass alignment (e.g. `"127.0.0.100"` falsely satisfies `"127.0.0.1"`).

---

## 1. Observation

### 1.1 Direct Code Observations in `luma/paas/migration/migrator.py`

#### A. `canonicalize_no_proxy` (Lines 78–88)
```python
78: def canonicalize_no_proxy(raw: str) -> str:
79:     """Parse comma/space/semicolon delimited NO_PROXY string into clean, deduplicated comma-separated string."""
80:     tokens: list[str] = []
81:     seen: set[str] = set()
82:     for chunk in re.split(r"[,;\s]+", raw or ""):
83:         item = chunk.strip().strip("'\"")
84:         if item and item not in seen:
85:             seen.add(item)
86:             tokens.append(item)
87:     return ",".join(tokens)
```
- **Line 84**: `item not in seen` performs case-sensitive deduplication. If `raw` contains `"localhost, LOCALHOST"`, `seen` stores `"localhost"`, and `"LOCALHOST"` is treated as a distinct token rather than deduplicated.

#### B. Substring Matching and Tokenization in `evaluate_node_self_heal` (Lines 199–213)
```python
199:     no_proxy_raw = str(node.get("noProxy") or "")
200:     no_proxy_canonical = canonicalize_no_proxy(no_proxy_raw)
201:     existing_tokens = set(no_proxy_canonical.split(",")) if no_proxy_canonical else set()
202: 
203:     req_hosts = list(required_no_proxy_hosts)
204:     if os_name == "darwin":
205:         for darwin_req in ("<local>", "localhost", "127.0.0.1"):
206:             if darwin_req not in req_hosts:
207:                 req_hosts.append(darwin_req)
208:     else:
209:         req_hosts = [h for h in req_hosts if h != "<local>"]
210: 
211:     missing_no_proxy = [h for h in req_hosts if not (h in no_proxy_raw or h in existing_tokens)]
212:     no_proxy_ok = len(missing_no_proxy) == 0
```
- **Line 211**: `not (h in no_proxy_raw or h in existing_tokens)` checks whether `h` is a substring of the raw, un-tokenized string `no_proxy_raw`.
- **Line 201**: `existing_tokens` is created as a `set(no_proxy_canonical.split(","))`, which uses exact case comparison and does not preserve insertion order.

#### C. Token Scrambling in Auto-Fix (Lines 251–257)
```python
251:         if not no_proxy_ok:
252:             no_proxy_ok = True
253:             canonical_tokens = list(existing_tokens)
254:             for m in req_hosts:
255:                 if m not in canonical_tokens:
256:                     canonical_tokens.append(m)
257:             no_proxy_canonical = ",".join(canonical_tokens)
```
- **Line 253**: `canonical_tokens = list(existing_tokens)` converts Python's unordered `set` into a `list`. This scrambles the original token ordering in `no_proxy_canonical`.
- **Line 255**: `if m not in canonical_tokens:` checks case-sensitively. If `existing_tokens` contained `"LOCALHOST"`, `"localhost"` is not found and is redundantly appended, resulting in `"LOCALHOST,...,localhost"`.

---

### 1.2 Empirical Reproduction Results

#### Reproduction 1: False Positive on `"127.0.0.100"`
Command:
```bash
./.venv/bin/python -c '
from luma.paas.migration.migrator import evaluate_node_self_heal
r = evaluate_node_self_heal({"osName": "darwin", "noProxy": "127.0.0.100,localhost,<local>,gcode.gaojiua.com,100.66.177.70:5000"})
print("r.no_proxy_aligned:", r.no_proxy_aligned)
print("127.0.0.1 in canonical:", "127.0.0.1" in r.no_proxy_canonical.split(","))
'
```
Output:
```
r.no_proxy_aligned: True
127.0.0.1 in canonical: False
```
*Verdict*: `no_proxy_aligned` falsely evaluates to `True` even though `127.0.0.1` is completely absent from the token list.

#### Reproduction 2: Auto-Fix Failure on False Positive
Command:
```bash
./.venv/bin/python -c '
from luma.paas.migration.migrator import evaluate_node_self_heal
r = evaluate_node_self_heal({"osName": "darwin", "noProxy": "127.0.0.100,localhost,<local>,gcode.gaojiua.com,100.66.177.70:5000"}, auto_fix=True)
print("r.no_proxy_aligned:", r.no_proxy_aligned)
print("127.0.0.1 in canonical:", "127.0.0.1" in r.no_proxy_canonical.split(","))
print("actions:", r.repaired_actions)
'
```
Output:
```
r.no_proxy_aligned: True
127.0.0.1 in canonical: False
actions: ()
```
*Verdict*: Because `no_proxy_aligned` is falsely `True`, auto-repair is skipped entirely (`actions: ()`), leaving localhost proxy bypass unaligned.

#### Reproduction 3: Substring Vulnerabilities with Other Hosts
Command:
```bash
./.venv/bin/python -c '
from luma.paas.migration.migrator import evaluate_node_self_heal
r = evaluate_node_self_heal({"osName": "linux", "noProxy": "my-localhost,not-gcode.gaojiua.com,100.66.177.70:50000"})
print("r.no_proxy_aligned:", r.no_proxy_aligned)
'
```
Output:
```
r.no_proxy_aligned: True
```
*Verdict*: Substring matching allows `"my-localhost"` to satisfy `"localhost"`, `"not-gcode.gaojiua.com"` to satisfy `"gcode.gaojiua.com"`, and `"100.66.177.70:50000"` to satisfy `"100.66.177.70:5000"`.

#### Reproduction 4: Case-Sensitivity and Token Scrambling
Command:
```bash
./.venv/bin/python -c '
from luma.paas.migration.migrator import evaluate_node_self_heal
r = evaluate_node_self_heal({"osName": "darwin", "noProxy": "LOCALHOST,127.0.0.1,GCODE.GAOJIUA.COM,100.66.177.70:5000,<LOCAL>"}, auto_fix=True)
print("r.no_proxy_canonical:", r.no_proxy_canonical)
print("repaired_actions:", r.repaired_actions)
'
```
Output:
```
r.no_proxy_canonical: 100.66.177.70:5000,GCODE.GAOJIUA.COM,LOCALHOST,127.0.0.1,<LOCAL>,gcode.gaojiua.com,<local>,localhost
repaired_actions: ('aligned NO_PROXY with internal Gitea, Zot Registry, and appended <local> and localhost for Darwin',)
```
*Verdict*: `100.66.177.70:5000` jumped to the front of the string due to set iteration order, and duplicate tokens `gcode.gaojiua.com`, `<local>`, and `localhost` were appended despite `GCODE.GAOJIUA.COM`, `<LOCAL>`, and `LOCALHOST` already being present.

---

## 2. Logic Chain

1. **Root Cause Analysis (Substring Match)**:
   - In Python, `"127.0.0.1" in "127.0.0.100"` evaluates to `True` because string `in` tests substring inclusion, not token identity.
   - Line 211 uses `h in no_proxy_raw`. Any substring match on raw text causes `h` to be omitted from `missing_no_proxy`.
   - Therefore, `missing_no_proxy` is empty (`len(missing_no_proxy) == 0`), making `no_proxy_ok = True`.
   - As a consequence, `evaluate_node_self_heal` reports that NO_PROXY is aligned when it is not, causing localhost traffic on Darwin builders to be routed through egress proxies (leading to connection drops or security leaks).

2. **Root Cause Analysis (Token Order & Case Insensitivity)**:
   - Hostnames and network domains in HTTP/URL standards (RFC 3986 §3.2.2, RFC 1123) and standard proxy bypass engines (including Luma's `luma/registry_proxy.py` line 27) are ASCII case-insensitive.
   - In `luma/paas/migration/migrator.py`:
     - `canonicalize_no_proxy` compared tokens case-sensitively in `seen.add(item)`.
     - `existing_tokens = set(no_proxy_canonical.split(","))` held case-sensitive tokens.
     - Membership checking did not lowercase before comparing.
     - `canonical_tokens = list(existing_tokens)` converted a `set` to a `list`, losing the deterministic token ordering established by `canonicalize_no_proxy`.

3. **Remediation Invariants**:
   - **Invariant 1 (Strict Token Membership)**: Membership checking must test whether `h` equals an existing token, completely eliminating `h in no_proxy_raw`.
   - **Invariant 2 (Case-Insensitive Comparison)**: Both existing tokens and required hosts must be normalized to lowercase for comparison, preventing duplicate entry generation for case variants (such as `LOCALHOST` vs `localhost`).
   - **Invariant 3 (Order Preservation)**: The initial token list must be parsed as an ordered list `canonical_tokens = [t for t in no_proxy_canonical.split(",") if t] if no_proxy_canonical else []`. When missing hosts are appended, they must be appended in order to `canonical_tokens`.
   - **Invariant 4 (Non-Destructive Casing)**: The original casing of existing tokens should be preserved in `no_proxy_canonical`, while missing required hosts are appended in standard lowercase format.

---

## 3. Caveats

- **Scope Boundary**: This investigation focuses strictly on Finding 2 (`noProxy` unanchored substring matching, tokenization, case-insensitivity, and ordering in `luma/paas/migration/migrator.py`). Finding 1 (`NodeSelfHealReport` mapping access) and Findings 3/4 (DaemonSet assertions and test coverage) are handled in parallel by peer fix tasks.
- **Darwin Special Token `<local>`**: `<local>` is Apple's domain-less bypass token. On non-Darwin nodes, `<local>` is filtered out. On Darwin nodes, it is required. The fix preserves this exact platform distinction while ensuring case-insensitive checks (`h.lower() != "<local>"`).

---

## 4. Conclusion & Proposed Remediation

### 4.1 Exact Code Changes in `luma/paas/migration/migrator.py`

#### Change 1: `canonicalize_no_proxy` (Lines 83–86)
Make token deduplication case-insensitive while preserving token casing:
```python
<<<<
        item = chunk.strip().strip("'\"")
        if item and item not in seen:
            seen.add(item)
            tokens.append(item)
====
        item = chunk.strip().strip("'\"")
        if item and item.lower() not in seen:
            seen.add(item.lower())
            tokens.append(item)
>>>>
```

#### Change 2: Tokenization and Token Membership in `evaluate_node_self_heal` (Lines 199–212)
Preserve token order in `canonical_tokens`, build `existing_tokens` as lowercased set, prevent case duplicates on Darwin `req_hosts`, and check `h.lower() not in existing_tokens`:
```python
<<<<
    no_proxy_raw = str(node.get("noProxy") or "")
    no_proxy_canonical = canonicalize_no_proxy(no_proxy_raw)
    existing_tokens = set(no_proxy_canonical.split(",")) if no_proxy_canonical else set()

    req_hosts = list(required_no_proxy_hosts)
    if os_name == "darwin":
        for darwin_req in ("<local>", "localhost", "127.0.0.1"):
            if darwin_req not in req_hosts:
                req_hosts.append(darwin_req)
    else:
        req_hosts = [h for h in req_hosts if h != "<local>"]

    missing_no_proxy = [h for h in req_hosts if not (h in no_proxy_raw or h in existing_tokens)]
    no_proxy_ok = len(missing_no_proxy) == 0
====
    no_proxy_raw = str(node.get("noProxy") or "")
    no_proxy_canonical = canonicalize_no_proxy(no_proxy_raw)
    canonical_tokens = [t for t in no_proxy_canonical.split(",") if t] if no_proxy_canonical else []
    existing_tokens = {t.lower() for t in canonical_tokens}

    req_hosts = list(required_no_proxy_hosts)
    if os_name == "darwin":
        req_hosts_lower = {h.lower() for h in req_hosts}
        for darwin_req in ("<local>", "localhost", "127.0.0.1"):
            if darwin_req.lower() not in req_hosts_lower:
                req_hosts.append(darwin_req)
                req_hosts_lower.add(darwin_req.lower())
    else:
        req_hosts = [h for h in req_hosts if h.lower() != "<local>"]

    missing_no_proxy = [h for h in req_hosts if h.lower() not in existing_tokens]
    no_proxy_ok = len(missing_no_proxy) == 0
>>>>
```

#### Change 3: Order Preservation during Auto-Fix (Lines 251–257)
Eliminate `canonical_tokens = list(existing_tokens)` set-to-list scrambling, and append missing tokens with case-insensitive checking:
```python
<<<<
        if not no_proxy_ok:
            no_proxy_ok = True
            canonical_tokens = list(existing_tokens)
            for m in req_hosts:
                if m not in canonical_tokens:
                    canonical_tokens.append(m)
            no_proxy_canonical = ",".join(canonical_tokens)
====
        if not no_proxy_ok:
            no_proxy_ok = True
            for m in req_hosts:
                if m.lower() not in existing_tokens:
                    existing_tokens.add(m.lower())
                    canonical_tokens.append(m)
            no_proxy_canonical = ",".join(canonical_tokens)
>>>>
```

---

### 4.2 Proposed Unit Test in `tests/test_paas_engine.py`

Add the following test method to class `MigrationReliabilityAndNodeSelfHealingTests` (insert after line 2211, right after `test_no_proxy_multiple_separators_and_duplicate_entries`):

```python
    def test_no_proxy_unanchored_substring_rejection_and_case_insensitivity(self) -> None:
        """Finding 2: Unanchored substring (127.0.0.100 vs 127.0.0.1) must not falsely satisfy alignment."""
        # Scenario 1: Unanchored substring IP false positive rejection (127.0.0.100 does not satisfy 127.0.0.1)
        node_substring = {
            "name": "macbook-pro",
            "osName": "darwin",
            "noProxy": "127.0.0.100,localhost,<local>,gcode.gaojiua.com,100.66.177.70:5000",
            "zotDirectReachable": True,
        }
        unfixed = evaluate_node_self_heal(node_substring, auto_fix=False)
        self.assertFalse(unfixed.no_proxy_aligned, "127.0.0.100 must not satisfy 127.0.0.1 requirement")
        self.assertFalse(unfixed.healthy)

        fixed = evaluate_node_self_heal(node_substring, auto_fix=True)
        self.assertTrue(fixed.no_proxy_aligned)
        tokens = fixed.no_proxy_canonical.split(",")
        self.assertIn("127.0.0.100", tokens)
        self.assertIn("127.0.0.1", tokens)
        self.assertEqual(tokens[0], "127.0.0.100", "Original token order must be preserved")

        # Scenario 2: Hostname substring false positive rejection (my-localhost, not-gcode)
        node_host_substring = {
            "name": "worker-1",
            "osName": "linux",
            "noProxy": "my-localhost,not-gcode.gaojiua.com,100.66.177.70:50000",
            "zotDirectReachable": True,
        }
        unfixed_host = evaluate_node_self_heal(node_host_substring, auto_fix=False)
        self.assertFalse(unfixed_host.no_proxy_aligned, "my-localhost/not-gcode must not satisfy requirements")

        # Scenario 3: Case-insensitive host alignment without duplicate tokens
        node_case = {
            "name": "macbook-pro",
            "osName": "darwin",
            "noProxy": "LOCALHOST,127.0.0.1,GCODE.GAOJIUA.COM,100.66.177.70:5000,<LOCAL>",
            "zotDirectReachable": True,
        }
        case_report = evaluate_node_self_heal(node_case, auto_fix=True)
        self.assertTrue(case_report.no_proxy_aligned)
        self.assertEqual(len(case_report.repaired_actions), 0, "No repair should be needed when tokens match case-insensitively")
        canonical_split = case_report.no_proxy_canonical.split(",")
        self.assertEqual(len(canonical_split), 5)
```

---

## 5. Verification Method

Once the implementer applies the diff, the fix can be independently verified through the following steps:

1. **Verify Substring Rejection in Python**:
   ```bash
   ./.venv/bin/python -c '
   from luma.paas.migration.migrator import evaluate_node_self_heal
   r = evaluate_node_self_heal({"osName": "darwin", "noProxy": "127.0.0.100,localhost,<local>,gcode.gaojiua.com,100.66.177.70:5000"}, auto_fix=False)
   assert r.no_proxy_aligned is False, "Must fail alignment when 127.0.0.1 is missing!"
   print("Unfixed verification: PASS")
   r_fixed = evaluate_node_self_heal({"osName": "darwin", "noProxy": "127.0.0.100,localhost,<local>,gcode.gaojiua.com,100.66.177.70:5000"}, auto_fix=True)
   assert r_fixed.no_proxy_aligned is True, "Must succeed after auto_fix!"
   tokens = r_fixed.no_proxy_canonical.split(",")
   assert "127.0.0.1" in tokens, "127.0.0.1 must be appended!"
   assert "127.0.0.100" in tokens, "127.0.0.100 must be preserved!"
   assert tokens[0] == "127.0.0.100", "Original ordering must be preserved!"
   print("Fixed verification: PASS")
   '
   ```

2. **Verify Case-Insensitivity and Duplicate Suppression in Python**:
   ```bash
   ./.venv/bin/python -c '
   from luma.paas.migration.migrator import evaluate_node_self_heal
   r = evaluate_node_self_heal({"osName": "darwin", "noProxy": "LOCALHOST,127.0.0.1,GCODE.GAOJIUA.COM,100.66.177.70:5000,<LOCAL>"}, auto_fix=True)
   assert r.no_proxy_aligned is True
   assert len(r.repaired_actions) == 0, "No repair action needed"
   assert len(r.no_proxy_canonical.split(",")) == 5, "No duplicate tokens added"
   print("Case insensitivity verification: PASS")
   '
   ```

3. **Run Full PaaS Engine Test Suite**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests -v
   ```
   *Expected outcome*: 0 failures, 0 errors.

4. **Run Full Repository Gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
   *Expected outcome*: Exit code 0 across all linters, type checks, and test suites.
