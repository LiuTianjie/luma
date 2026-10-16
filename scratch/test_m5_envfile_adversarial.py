#!/usr/bin/env python3
"""Adversarial stress-test for luma/envfile.py setting loading and edge cases."""

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from luma.envfile import (
    LUMA_SETTING_NAMES,
    LUMA_CREDENTIAL_NAMES,
    is_luma_setting,
    load_luma_settings,
    load_env_file,
    parse_env_file,
    _parse_env_value,
)
from luma.errors import LumaError

def test_luma_setting_membership():
    print(">>> 1. Verifying LUMA_SETTING_NAMES & is_luma_setting membership...")
    assert "CLOUDFLARE_ZONE_ID" in LUMA_SETTING_NAMES, "CLOUDFLARE_ZONE_ID missing from LUMA_SETTING_NAMES!"
    assert is_luma_setting("CLOUDFLARE_ZONE_ID") is True
    assert is_luma_setting("CLOUDFLARE_API_TOKEN") is True
    assert is_luma_setting("EGRESS_SUBSCRIPTION_URL") is True
    assert is_luma_setting("TAILSCALE_AUTHKEY") is True
    assert is_luma_setting("TRAEFIK_ACME_EMAIL") is True

    # Generic LUMA_ settings
    assert is_luma_setting("LUMA_CUSTOM_SETTING") is True

    # Credentials must be excluded from implicit .env loading
    for cred in ["LUMA_CONTROL_URL", "LUMA_DEPLOY_TOKEN", "LUMA_CONTROL_CONTEXT"]:
        assert cred in LUMA_CREDENTIAL_NAMES
        assert is_luma_setting(cred) is False, f"Credential '{cred}' must NOT be treated as a luma setting!"

    # Non-Luma application variables
    for app_var in ["DATABASE_URL", "SECRET_KEY", "PORT", "REDIS_HOST"]:
        assert is_luma_setting(app_var) is False
    print("PASS: is_luma_setting classifications match exact specification.")

def test_edge_case_cloudflare_zone_id_loading():
    print(">>> 2. Stress-testing edge-case inputs for CLOUDFLARE_ZONE_ID in load_luma_settings...")

    test_cases = [
        # (case_name, raw_line, expected_value)
        ("standard", "CLOUDFLARE_ZONE_ID=023e105f4ecef8ad9ca31a8372d0c353", "023e105f4ecef8ad9ca31a8372d0c353"),
        ("leading_trailing_spaces", "   CLOUDFLARE_ZONE_ID   =   023e105f4ecef8ad9ca31a8372d0c353   ", "023e105f4ecef8ad9ca31a8372d0c353"),
        ("export_prefix", "export CLOUDFLARE_ZONE_ID=023e105f4ecef8ad9ca31a8372d0c353", "023e105f4ecef8ad9ca31a8372d0c353"),
        ("export_with_spaces", "  export   CLOUDFLARE_ZONE_ID  =  023e105f4ecef8ad9ca31a8372d0c353  ", "023e105f4ecef8ad9ca31a8372d0c353"),
        ("double_quotes", 'CLOUDFLARE_ZONE_ID="023e105f4ecef8ad9ca31a8372d0c353"', "023e105f4ecef8ad9ca31a8372d0c353"),
        ("single_quotes", "CLOUDFLARE_ZONE_ID='023e105f4ecef8ad9ca31a8372d0c353'", "023e105f4ecef8ad9ca31a8372d0c353"),
        ("quotes_with_internal_spaces", 'CLOUDFLARE_ZONE_ID="  zone-with-spaces  "', "  zone-with-spaces  "),
        ("unquoted_inline_comment", "CLOUDFLARE_ZONE_ID=023e105f4ecef8ad9ca31a8372d0c353 # Production Cloudflare Zone", "023e105f4ecef8ad9ca31a8372d0c353"),
        ("double_quoted_inline_comment", 'CLOUDFLARE_ZONE_ID="023e105f4ecef8ad9ca31a8372d0c353" # Production Cloudflare Zone', "023e105f4ecef8ad9ca31a8372d0c353"),
        ("single_quoted_inline_comment", "CLOUDFLARE_ZONE_ID='023e105f4ecef8ad9ca31a8372d0c353' # Production Cloudflare Zone", "023e105f4ecef8ad9ca31a8372d0c353"),
        ("hash_inside_quotes", 'CLOUDFLARE_ZONE_ID="zone#id#with#hash"', "zone#id#with#hash"),
        ("equals_inside_quotes", 'CLOUDFLARE_ZONE_ID="zone=id=with=equals"', "zone=id=with=equals"),
        ("empty_unquoted", "CLOUDFLARE_ZONE_ID=", ""),
        ("empty_unquoted_with_spaces", "CLOUDFLARE_ZONE_ID=   ", ""),
        ("empty_double_quotes", 'CLOUDFLARE_ZONE_ID=""', ""),
        ("empty_single_quotes", "CLOUDFLARE_ZONE_ID=''", ""),
        ("empty_quotes_with_inline_comment", 'CLOUDFLARE_ZONE_ID="" # empty zone id', ""),
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        for name, line_content, expected in test_cases:
            env_file = Path(tmpdir) / f".env.{name}"
            env_file.write_text(f"{line_content}\n", encoding="utf-8")

            # Clean environment before test
            os.environ.pop("CLOUDFLARE_ZONE_ID", None)

            warnings = load_luma_settings(env_file)
            assert not warnings, f"Unexpected warnings for case '{name}': {warnings}"
            assert "CLOUDFLARE_ZONE_ID" in os.environ, f"Case '{name}' failed: CLOUDFLARE_ZONE_ID not loaded into os.environ!"
            actual = os.environ["CLOUDFLARE_ZONE_ID"]
            assert actual == expected, f"Case '{name}' mismatch: expected {expected!r}, got {actual!r}"
            print(f"  Case '{name}': successfully parsed as {actual!r}")

    print("PASS: All 17 edge cases for CLOUDFLARE_ZONE_ID passed.")

def test_mixing_credentials_and_app_vars():
    print(">>> 3. Verifying isolation when mixing credentials and application variables...")

    env_content = """
# App configuration
DATABASE_URL=postgres://app:secret@db.internal:5432/production
REDIS_URL=redis://10.0.0.1:6379/0
PORT=8080

# Luma credentials (MUST NOT leak into os.environ via load_luma_settings)
LUMA_CONTROL_URL=https://control.internal:8443
LUMA_DEPLOY_TOKEN=super-secret-deploy-token-12345
LUMA_CONTROL_CONTEXT=internal-prod

# Luma cluster settings (MUST be loaded into os.environ)
CLOUDFLARE_ZONE_ID=023e105f4ecef8ad9ca31a8372d0c353
CLOUDFLARE_API_TOKEN=cf-api-token-987654321
EGRESS_SUBSCRIPTION_URL=https://egress.example.com/sub.txt
TAILSCALE_AUTHKEY=tskey-auth-abcdef
TRAEFIK_ACME_EMAIL=ops@example.com
LUMA_CUSTOM_SETTING=custom-value-xyz
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        env_file = Path(tmpdir) / ".env"
        env_file.write_text(env_content, encoding="utf-8")

        # Clear relevant env vars
        keys_to_clean = [
            "DATABASE_URL", "REDIS_URL", "PORT",
            "LUMA_CONTROL_URL", "LUMA_DEPLOY_TOKEN", "LUMA_CONTROL_CONTEXT",
            "CLOUDFLARE_ZONE_ID", "CLOUDFLARE_API_TOKEN", "EGRESS_SUBSCRIPTION_URL",
            "TAILSCALE_AUTHKEY", "TRAEFIK_ACME_EMAIL", "LUMA_CUSTOM_SETTING",
        ]
        for k in keys_to_clean:
            os.environ.pop(k, None)

        warnings = load_luma_settings(env_file)
        assert len(warnings) == 0, f"Unexpected warnings: {warnings}"

        # 1. Assert Luma settings ARE in os.environ
        assert os.environ.get("CLOUDFLARE_ZONE_ID") == "023e105f4ecef8ad9ca31a8372d0c353"
        assert os.environ.get("CLOUDFLARE_API_TOKEN") == "cf-api-token-987654321"
        assert os.environ.get("EGRESS_SUBSCRIPTION_URL") == "https://egress.example.com/sub.txt"
        assert os.environ.get("TAILSCALE_AUTHKEY") == "tskey-auth-abcdef"
        assert os.environ.get("TRAEFIK_ACME_EMAIL") == "ops@example.com"
        assert os.environ.get("LUMA_CUSTOM_SETTING") == "custom-value-xyz"

        # 2. Assert Luma credentials ARE NOT in os.environ
        assert "LUMA_CONTROL_URL" not in os.environ, "LUMA_CONTROL_URL leaked into os.environ!"
        assert "LUMA_DEPLOY_TOKEN" not in os.environ, "LUMA_DEPLOY_TOKEN leaked into os.environ!"
        assert "LUMA_CONTROL_CONTEXT" not in os.environ, "LUMA_CONTROL_CONTEXT leaked into os.environ!"

        # 3. Assert application variables ARE NOT in os.environ
        assert "DATABASE_URL" not in os.environ, "DATABASE_URL leaked into os.environ!"
        assert "REDIS_URL" not in os.environ, "REDIS_URL leaked into os.environ!"
        assert "PORT" not in os.environ, "PORT leaked into os.environ!"

    print("PASS: Credentials and non-Luma application variables successfully isolated.")

def test_unclosed_quotes_and_resilience():
    print(">>> 4. Testing unclosed quotes and error resilience in load_luma_settings...")

    env_content = """
# Valid setting before malformed line
CLOUDFLARE_ZONE_ID=valid-zone-id

# Malformed unclosed quote on another Luma setting
CLOUDFLARE_API_TOKEN="unclosed-quote-without-ending

# Setting after malformed line
TRAEFIK_ACME_EMAIL=admin@example.com
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        env_file = Path(tmpdir) / ".env"
        env_file.write_text(env_content, encoding="utf-8")

        os.environ.pop("CLOUDFLARE_ZONE_ID", None)
        os.environ.pop("CLOUDFLARE_API_TOKEN", None)
        os.environ.pop("TRAEFIK_ACME_EMAIL", None)

        # load_luma_settings must NOT crash; it must return warnings
        warnings = load_luma_settings(env_file)
        assert len(warnings) == 1, f"Expected 1 warning for unclosed quote, got: {warnings}"
        assert "ignored 1 unreadable Luma setting(s)" in warnings[0]

        # Valid settings must still be loaded
        assert os.environ.get("CLOUDFLARE_ZONE_ID") == "valid-zone-id"
        assert os.environ.get("TRAEFIK_ACME_EMAIL") == "admin@example.com"
        # Malformed setting must not be loaded
        assert "CLOUDFLARE_API_TOKEN" not in os.environ

    print("PASS: Unclosed quotes gracefully caught as warnings without dropping valid settings.")

def test_existing_environment_variable_precedence():
    print(">>> 5. Testing existing environment variable precedence...")

    with tempfile.TemporaryDirectory() as tmpdir:
        env_file = Path(tmpdir) / ".env"
        env_file.write_text("CLOUDFLARE_ZONE_ID=from-file-zone\n", encoding="utf-8")

        # Case A: os.environ already has a value -> load_luma_settings preserves it
        os.environ["CLOUDFLARE_ZONE_ID"] = "pre-existing-zone"
        load_luma_settings(env_file)
        assert os.environ["CLOUDFLARE_ZONE_ID"] == "pre-existing-zone", "Existing environment variable was overwritten!"

        # Case B: load_env_file with override=True -> updates it
        load_env_file(env_file, override=True)
        assert os.environ["CLOUDFLARE_ZONE_ID"] == "from-file-zone", "override=True failed to update value!"

    print("PASS: Environment variable precedence respected.")

if __name__ == "__main__":
    test_luma_setting_membership()
    test_edge_case_cloudflare_zone_id_loading()
    test_mixing_credentials_and_app_vars()
    test_unclosed_quotes_and_resilience()
    test_existing_environment_variable_precedence()
    print("\nALL OBJECTIVE 3 TESTS PASSED SUCCESSFULLY!")
