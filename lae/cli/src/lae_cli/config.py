from __future__ import annotations

import getpass
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Mapping, TextIO

from .errors import CliError


_DEPLOY_TOKEN = re.compile(
    r"^lae_dt_[0-9A-HJKMNP-TV-Z]{10}_[A-Za-z0-9_-]{43}$"
)


@dataclass(frozen=True, slots=True)
class DeployCredential:
    value: str

    def __repr__(self) -> str:
        return "DeployCredential(value=<redacted>)"


def api_url(environ: Mapping[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    raw = values.get("LAE_API_URL", "https://lae-api.itool.tech/v1").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise _configuration_error("LAE_API_URL is invalid") from exc
    local_host = (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
    if (
        parsed.scheme not in ({"https", "http"} if local_host else {"https"})
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise _configuration_error(
            "LAE_API_URL must be HTTPS (HTTP is allowed only for localhost)"
        )
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    if path != "/v1":
        raise _configuration_error("LAE_API_URL path must be /v1")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc.lower(), path, "", "")
    )


def deploy_credential(
    *,
    from_stdin: bool,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
) -> DeployCredential:
    values = os.environ if environ is None else environ
    source = sys.stdin if stdin is None else stdin
    if from_stdin:
        if getattr(source, "isatty", lambda: False)():
            token = getpass.getpass("LAE deploy token: ")
        else:
            token = source.readline(512).rstrip("\r\n")
    else:
        token = values.get("LAE_DEPLOY_TOKEN", "")
    if not isinstance(token, str) or not _DEPLOY_TOKEN.fullmatch(token):
        raise CliError(
            "LAE_UNAUTHENTICATED",
            "Configure a valid deploy token through LAE_DEPLOY_TOKEN or --token-stdin.",
            3,
        )
    return DeployCredential(token)


def token_is_configured(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return _DEPLOY_TOKEN.fullmatch(values.get("LAE_DEPLOY_TOKEN", "")) is not None


def _configuration_error(message: str) -> CliError:
    return CliError("LAE_CLI_CONFIGURATION_INVALID", message, 2)
