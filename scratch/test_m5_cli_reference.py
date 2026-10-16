#!/usr/bin/env python3
"""Adversarial stress-test for CLI reference and help command behavior."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_BIN = REPO_ROOT / ".venv" / "bin" / "python"
LUMA_BIN = REPO_ROOT / ".venv" / "bin" / "luma"

def test_cli_reference_check():
    print(">>> 1. Running generate-cli-reference.py --check...")
    res = subprocess.run([str(PYTHON_BIN), "scripts/generate-cli-reference.py", "--check"], cwd=REPO_ROOT, capture_output=True, text=True)
    assert res.returncode == 0, f"--check failed!\nStdout: {res.stdout}\nStderr: {res.stderr}"
    print("PASS: scripts/generate-cli-reference.py --check passed.")

def test_luma_help_suppression():
    print(">>> 2. Verifying luma --help does not contain migrate command...")
    res = subprocess.run([str(LUMA_BIN), "--help"], cwd=REPO_ROOT, capture_output=True, text=True)
    assert res.returncode == 0, f"luma --help failed!\nStderr: {res.stderr}"
    help_text = res.stdout

    # Check that standard commands are present
    for cmd in ["init", "deploy", "compose", "build", "status", "bootstrap"]:
        assert cmd in help_text, f"Expected standard command '{cmd}' in luma --help!"

    # Check that 'migrate' is NOT present as a command choice in help
    lines = help_text.splitlines()
    command_lines = [l.strip() for l in lines if l.strip().startswith("migrate ")]
    assert len(command_lines) == 0, f"'migrate' found in luma --help: {command_lines}"

    # Also check exact word matching for commands in help text sections
    words_at_start = [l.split()[0] for l in lines if l.startswith("  ") and len(l.split()) > 0]
    assert "migrate" not in words_at_start, f"'migrate' found in command list: {words_at_start}"
    print("PASS: 'migrate' is completely hidden from luma --help.")

def test_luma_migrate_help_subcommands():
    print(">>> 3. Verifying luma migrate --help exposes subcommands...")
    res = subprocess.run([str(LUMA_BIN), "migrate", "--help"], cwd=REPO_ROOT, capture_output=True, text=True)
    assert res.returncode == 0, f"luma migrate --help failed!\nStderr: {res.stderr}"
    help_text = res.stdout

    expected_subcommands = ["batches", "plan", "shadow", "cutover", "abort", "finalize"]
    for sub in expected_subcommands:
        assert sub in help_text, f"Expected subcommand '{sub}' in luma migrate --help!"
        # Check each subcommand --help directly
        sub_res = subprocess.run([str(LUMA_BIN), "migrate", sub, "--help"], cwd=REPO_ROOT, capture_output=True, text=True)
        assert sub_res.returncode == 0, f"luma migrate {sub} --help failed!\nStderr: {sub_res.stderr}"
    print(f"PASS: All {len(expected_subcommands)} migrate subcommands verified via --help.")

def test_cli_reference_tampering():
    print(">>> 4. Testing adversarial tampering on docs/luma-cli-reference.md...")
    ref_file = REPO_ROOT / "docs" / "luma-cli-reference.md"
    original_content = ref_file.read_text(encoding="utf-8")
    try:
        ref_file.write_text(original_content + "\n<!-- Tampered line -->\n", encoding="utf-8")
        res = subprocess.run([str(PYTHON_BIN), "scripts/generate-cli-reference.py", "--check"], cwd=REPO_ROOT, capture_output=True, text=True)
        assert res.returncode != 0, "generate-cli-reference.py --check failed to catch tampering!"
        assert "CLI reference is stale" in res.stderr
        print("PASS: generate-cli-reference.py --check successfully caught tampering.")
    finally:
        ref_file.write_text(original_content, encoding="utf-8")

    # Re-verify clean
    res_clean = subprocess.run([str(PYTHON_BIN), "scripts/generate-cli-reference.py", "--check"], cwd=REPO_ROOT, capture_output=True, text=True)
    assert res_clean.returncode == 0

if __name__ == "__main__":
    test_cli_reference_check()
    test_luma_help_suppression()
    test_luma_migrate_help_subcommands()
    test_cli_reference_tampering()
    print("\nALL OBJECTIVE 2 TESTS PASSED SUCCESSFULLY!")
