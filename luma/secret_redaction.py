"""Write-only credentials in administrative configuration/history responses."""
from __future__ import annotations

import re
from typing import Any

import yaml

from .errors import LumaError

_CREDENTIAL = re.compile(r"(?:password|passwd|secret|token|apikey|accesskey|privatekey|authorization|credential)$", re.I)
_REFERENCE = re.compile(r"\$\{[^}]+\}\Z")
_PRIVATE = {"queuePayload", "privateRequest", "retryEnvSecrets", "registryAuth", "gitToken", "envSecrets"}


def _value(value: Any) -> Any:
    if value is None or value == "":
        return value
    if isinstance(value, str) and _REFERENCE.fullmatch(value):
        return value
    return "<redacted>"


def redact_document(value: Any, *, field: str = "") -> Any:
    """Preserve useful metadata without exposing stored env/credential values.

    This is deliberately applied to read-only views, not enrollment or lease
    responses whose authenticated caller is meant to receive a scoped secret.
    """
    if field in {"manifest", "composeContent"} and isinstance(value, str):
        try:
            document = yaml.safe_load(value)
        except yaml.YAMLError:
            return "<redacted: invalid configuration>"
        if isinstance(document, (dict, list)):
            return yaml.safe_dump(redact_document(document), sort_keys=False, allow_unicode=True)
        return value
    if field.lower() in {"env", "environment"}:
        if isinstance(value, dict):
            return {str(key): _value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [str(item).partition("=")[0] + "=<redacted>" for item in value]
    if field.lower() in {"command", "args", "entrypoint"}:
        if isinstance(value, list):
            return [_value(item) for item in value]
        return _value(value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in _PRIVATE:
                if key == "envSecrets" and isinstance(item, dict):
                    result["envSecretNames"] = sorted(str(name) for name in item)
                continue
            compact = re.sub(r"[^a-zA-Z]", "", str(key))
            if _CREDENTIAL.search(compact) and isinstance(item, str):
                result[key] = _value(item)
            else:
                result[key] = redact_document(item, field=str(key))
        return result
    if isinstance(value, list):
        return [redact_document(item) for item in value]
    return value


def restore_configuration(text: str, previous: str) -> str:
    """Round-trip write-only fields when editing this application's config."""
    if "<redacted>" not in text:
        return text
    def restore(value, old):
        if value == "<redacted>":
            if old is None:
                raise LumaError("a redacted configuration value has no saved value; supply the new value or a secret reference")
            return old
        if isinstance(value, dict):
            return {key: restore(item, (old if isinstance(old, dict) else {}).get(key)) for key, item in value.items()}
        if isinstance(value, list):
            source = old if isinstance(old, list) else []
            return [restore(item, source[index] if index < len(source) else None) for index, item in enumerate(value)]
        if isinstance(value, str) and value.endswith("=<redacted>"):
            name = value.partition("=")[0]
            if not isinstance(old, str) or old.partition("=")[0] != name:
                raise LumaError("a redacted environment entry moved; supply its secret reference or value")
            return old
        return value
    try:
        return yaml.safe_dump(restore(yaml.safe_load(text), yaml.safe_load(previous)), sort_keys=False, allow_unicode=True)
    except yaml.YAMLError:
        raise LumaError("cannot restore write-only values from invalid configuration") from None
