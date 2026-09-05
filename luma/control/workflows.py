"""Application deployment recipes, retained independently of build/event history."""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlsplit

from ..deploy_workflow import comparison_fields, safe_url, validate_recipe
from ..errors import LumaError
from ..service import slugify
from .state import load_state, mutate_state, require_token


def _repo_key(value: str) -> str:
    value = safe_url(value.strip())
    if not value:
        return ""
    if "://" not in value and re.match(r"[^/]+@[^:]+:", value):
        value = "ssh://" + value.replace(":", "/", 1)
    if "://" not in value:
        value = "https://github.com/" + value
    parts = urlsplit(value)
    return ((parts.hostname or "").lower() + "/" + parts.path.strip("/").removesuffix(".git")).lower()


def _recipe_repo(state: dict[str, Any], recipe: dict[str, Any]) -> str:
    fields = comparison_fields(recipe)
    url = fields.get("repo", "") or fields.get("repo_url", "")
    if url:
        return _repo_key(url)
    provider_id = fields.get("provider_id", "")
    repository = fields.get("repository", "")
    if provider_id and repository:
        provider = (state.get("gitProviders") or {}).get(provider_id) or {}
        base = provider.get("baseUrl") or ("https://github.com" if provider_id.startswith("github:") else "")
        if base:
            return _repo_key(base.rstrip("/") + "/" + repository)
        return provider_id + ":" + repository.lower()
    return ""


def _selector(state: dict[str, Any], body: dict[str, Any], recipe: dict[str, Any]) -> dict[str, str]:
    raw = body.get("selector") or {}
    if not isinstance(raw, dict):
        raise LumaError("workflow selector must be an object")
    name = raw.get("name") or ""
    if name:
        name = _name(name)
    repo_url = raw.get("repoUrl") or ""
    if not isinstance(repo_url, str) or len(repo_url) > 4096:
        raise LumaError("invalid workflow repository")
    return {"name": name, "repoKey": _recipe_repo(state, recipe) or _repo_key(repo_url)}


def _matching_records(state: dict[str, Any], selector: dict[str, str]) -> list[dict[str, Any]]:
    records = state.get("deploymentWorkflows") or {}
    name = selector["name"]
    if name and slugify(name) in records:
        return [records[slugify(name)]]
    if name:
        return []
    repo_key = selector["repoKey"]
    return [row for row in records.values() if repo_key and row.get("repoKey") == repo_key]


def handle_workflow_check(token: str, body: dict[str, Any]) -> dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    recipe = validate_recipe(body.get("recipe"))
    selector = _selector(state, body, recipe)
    records = _matching_records(state, selector)
    if len(records) > 1:
        # A monorepo may deliberately use different workflows per Compose sidecar.
        fields = comparison_fields(recipe)
        sidecar = fields.get("compose_sidecar") or fields.get("sidecar")
        matches = [row for row in records if sidecar and sidecar == (
            comparison_fields(row["recipe"]).get("compose_sidecar") or comparison_fields(row["recipe"]).get("sidecar")
        )]
        if len(matches) == 1:
            records = matches
        else:
            raise LumaError("multiple application workflows match this repository; select one with --workflow-app: " + ", ".join(row["name"] for row in records))
    if not records:
        return {"status": "unrecorded", "differences": [], "selector": selector}
    previous = records[0]
    before = comparison_fields(previous["recipe"])
    after = comparison_fields(recipe)
    # Provider-backed and URL-backed imports can name the same repository.
    for fields, candidate, fallback_repo in ((before, previous["recipe"], previous.get("repoKey", "")), (after, recipe, selector["repoKey"])):
        for key in ("provider_id", "repository", "repo", "repo_url"):
            fields.pop(key, None)
        fields["repository"] = _recipe_repo(state, candidate) or fallback_repo
    differences = [
        {"field": key, "previous": before.get(key), "requested": after.get(key)}
        for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)
    ]
    return {
        "status": "confirmation-required" if differences else "match",
        "workflow": previous,
        "differences": differences,
        "selector": selector,
    }


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise LumaError("workflow application name is required (maximum 200 characters)")
    return value.strip()


def handle_workflow_list(token: str) -> dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    records = state.get("deploymentWorkflows") or {}
    return {"workflows": sorted(records.values(), key=lambda row: row["name"])}


def handle_workflow_get(token: str, name: str) -> dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    name = _name(name)
    record = (state.get("deploymentWorkflows") or {}).get(slugify(name))
    if not record:
        raise LumaError(f"no deployment workflow recorded for {name}; the next successful CLI deployment will create it, or use luma workflow record")
    return {"workflow": record}


def handle_workflow_record(token: str, body: dict[str, Any]) -> dict[str, Any]:
    # Authenticate before parsing anything supplied by the caller.
    require_token(load_state(), token, token_type="deploy")
    name = _name(body.get("name"))
    recipe = validate_recipe(body.get("recipe"))
    source = body.get("source", "manual")
    if source not in {"manual", "cli-success"}:
        raise LumaError("workflow source must be manual or cli-success")
    note = body.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > 8000):
        raise LumaError("workflow note must be text (maximum 8000 characters)")
    evidence = body.get("evidence") or {}
    if not isinstance(evidence, dict):
        raise LumaError("workflow evidence must be an object")
    evidence = {
        key: safe_url(value)[:4096]
        for key, value in evidence.items()
        if key in {"buildId", "image", "revision"} and isinstance(value, str)
    }

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        require_token(state, token, token_type="deploy")
        records = state.setdefault("deploymentWorkflows", {})
        previous = records.get(slugify(name)) or {}
        expected_revision = body.get("expectedRevision")
        if expected_revision is not None and expected_revision != previous.get("revision", 0):
            raise LumaError("deployment workflow changed while this deployment was running; the newer record was preserved")
        selector = _selector(state, body, recipe)
        record = {
            "name": name,
            "slug": slugify(name),
            "recipe": recipe,
            "source": source,
            "repoKey": selector["repoKey"] or previous.get("repoKey", ""),
            "note": note if note is not None else previous.get("note", ""),
            "updatedAt": int(time.time()),
            "revision": previous.get("revision", 0) + 1,
        }
        # Keep the actual prior recipe with the success: a manually edited recipe
        # has not itself been verified by that earlier deployment.
        if previous.get("lastSuccess"):
            record["lastSuccess"] = previous["lastSuccess"]
        if source == "cli-success":
            record["lastSuccess"] = {"at": record["updatedAt"], "recipe": recipe, **evidence}
        records[slugify(name)] = record
        return {"workflow": record}

    return mutate_state(mutate)
