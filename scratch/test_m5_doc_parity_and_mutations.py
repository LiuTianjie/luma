#!/usr/bin/env python3
"""Adversarial stress-test for Milestone 5 bilingual documentation and check-site.py integrity."""

import copy
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EN_DOC = REPO_ROOT / "docs" / "migration-guide.md"
ZH_DOC = REPO_ROOT / "docs" / "migration-guide.zh-CN.md"

def extract_anchors(text: str) -> list[str]:
    return re.findall(r'\{#([^}]+)\}', text)

def extract_code_fences(text: str) -> list[str]:
    return re.findall(r'^```[^\n]*\n.*?^```[^\n]*', text, re.M | re.S)

def test_anchor_parity_1_to_1():
    print(">>> 1. Verifying heading anchors 1:1 parity...")
    en_text = EN_DOC.read_text(encoding="utf-8")
    zh_text = ZH_DOC.read_text(encoding="utf-8")

    en_anchors = extract_anchors(en_text)
    zh_anchors = extract_anchors(zh_text)

    print(f"EN Anchors ({len(en_anchors)}): {en_anchors}")
    print(f"ZH Anchors ({len(zh_anchors)}): {zh_anchors}")

    assert len(en_anchors) > 0, "No anchors found in English document!"
    assert en_anchors == zh_anchors, f"Anchors do not match 1:1!\nEN: {en_anchors}\nZH: {zh_anchors}"
    print("PASS: Heading anchors match 1:1 in exact sequence.")

def test_code_fences_byte_for_byte():
    print(">>> 2. Verifying all 5 code fences match byte-for-byte...")
    en_text = EN_DOC.read_text(encoding="utf-8")
    zh_text = ZH_DOC.read_text(encoding="utf-8")

    en_fences = extract_code_fences(en_text)
    zh_fences = extract_code_fences(zh_text)

    print(f"EN Code Fences count: {len(en_fences)}")
    print(f"ZH Code Fences count: {len(zh_fences)}")

    assert len(en_fences) == 5, f"Expected exactly 5 code fences in EN, found {len(en_fences)}"
    assert len(zh_fences) == 5, f"Expected exactly 5 code fences in ZH, found {len(zh_fences)}"

    for idx, (en_f, zh_f) in enumerate(zip(en_fences, zh_fences), start=1):
        assert en_f == zh_f, f"Fence #{idx} differs!\nEN:\n{en_f}\nZH:\n{zh_f}"
        print(f"  Fence #{idx} ({en_f.splitlines()[0]}): byte-for-byte identical ({len(en_f.encode('utf-8'))} bytes)")

    print("PASS: All 5 code fences match byte-for-byte.")

def run_site_build_and_check(site_dir: Path) -> subprocess.CompletedProcess:
    # Build
    build_cmd = [
        str(REPO_ROOT / ".venv" / "bin" / "python"),
        str(REPO_ROOT / "scripts" / "build-site.py"),
        "--output",
        str(site_dir),
    ]
    res_build = subprocess.run(build_cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    if res_build.returncode != 0:
        return res_build
    # Check
    check_cmd = [
        str(REPO_ROOT / ".venv" / "bin" / "python"),
        str(REPO_ROOT / "scripts" / "check-site.py"),
        str(site_dir),
    ]
    return subprocess.run(check_cmd, capture_output=True, text=True, cwd=REPO_ROOT)

def test_mutation_adversarial_tampering():
    print(">>> 3. Testing adversarial mutations and tampering against check-site.py...")

    original_zh_text = ZH_DOC.read_text(encoding="utf-8")
    test_site_dir = REPO_ROOT / "scratch" / "test-mutation-site"

    try:
        # Mutation 1: Omit an anchor from ZH document
        print("  Testing Mutation 1: Omit anchor `{#post-migration-nomad-decommission}` in ZH...")
        mutated_zh = original_zh_text.replace("{#post-migration-nomad-decommission}", "")
        ZH_DOC.write_text(mutated_zh, encoding="utf-8")
        res = run_site_build_and_check(test_site_dir)
        assert res.returncode != 0, "check-site.py failed to catch omitted anchor!"
        assert "translation loses heading anchors" in res.stderr or "translation loses heading anchors" in res.stdout, (
            f"Expected anchor loss error, got:\nStdout: {res.stdout}\nStderr: {res.stderr}"
        )
        print("  PASS: check-site.py successfully caught anchor omission!")

        # Mutation 2: Code block tampering (alter 1 character in code block)
        print("  Testing Mutation 2: Tamper with code block in ZH (nomad -> nomad-tampered)...")
        mutated_zh = original_zh_text.replace("sudo systemctl disable --now nomad", "sudo systemctl disable --now nomad-tampered")
        ZH_DOC.write_text(mutated_zh, encoding="utf-8")
        res = run_site_build_and_check(test_site_dir)
        assert res.returncode != 0, "check-site.py failed to catch tampered code block!"
        assert "translated command/code examples differ" in res.stderr or "translated command/code examples differ" in res.stdout, (
            f"Expected code block diff error, got:\nStdout: {res.stdout}\nStderr: {res.stderr}"
        )
        print("  PASS: check-site.py successfully caught code block tampering!")

        # Mutation 3: Change code block language tag (bash -> sh)
        print("  Testing Mutation 3: Alter code block language tag (```bash -> ```sh)...")
        mutated_zh = original_zh_text.replace("```bash\n# Dry-run preflight checks", "```sh\n# Dry-run preflight checks", 1)
        ZH_DOC.write_text(mutated_zh, encoding="utf-8")
        res = run_site_build_and_check(test_site_dir)
        assert res.returncode != 0, "check-site.py failed to catch code fence language tag change!"
        assert "translated command/code examples differ" in res.stderr or "translated command/code examples differ" in res.stdout
        print("  PASS: check-site.py successfully caught code fence language alteration!")

    finally:
        # Restore pristine content
        ZH_DOC.write_text(original_zh_text, encoding="utf-8")
        if test_site_dir.exists():
            shutil.rmtree(test_site_dir, ignore_errors=True)

    # Verify baseline is fully restored
    res_clean = run_site_build_and_check(REPO_ROOT / "scratch" / "luma-site")
    assert res_clean.returncode == 0, f"Failed to restore clean site state! Stderr:\n{res_clean.stderr}"
    print("PASS: Baseline documentation restored and clean build passes.")

if __name__ == "__main__":
    test_anchor_parity_1_to_1()
    test_code_fences_byte_for_byte()
    test_mutation_adversarial_tampering()
    print("\nALL OBJECTIVE 1 TESTS PASSED SUCCESSFULLY!")
