"""Task-bound secret delivery through Nomad's native encrypted Variables.

Nomad 2.1's secret hook resolves values in memory using the allocation's signed
identity. Job Env and Docker auth/arguments retain references, not secret values.
Only the exact job/group/task Variable is populated: no job-wide shared grants
and no bearer token or decryption key is delivered to an application.
"""
from __future__ import annotations

import copy
import json
import re
import urllib.parse
from typing import Any, Mapping

from .errors import LumaError

SECRET_NAME = "luma"
MAX_VARIABLE_BYTES = 65536
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_REFERENCE = re.compile(r"\$\{secret\.luma\.([a-zA-Z0-9_]+)\}")
_RUNTIME = re.compile(r"\$\{(?:NOMAD_[A-Za-z0-9_]+|(?:node|attr|meta)\.[A-Za-z0-9_.-]+)\}\Z")
_INTERPOLATION = re.compile(r"(?<!\$)(\$\{[^}]+\})")
# These values are installed by prepare_job and contain paths/transport options,
# never credential contents. Keeping them stable makes repeated preparation safe.
def _public_transport(name: str, value: str, job_id: str, task_name: str) -> bool:
    if (job_id, task_name) not in {("luma-control", "luma-control"), ("traefik", "traefik"), ("luma-observe", "nomad-exporter"), ("luma-observe", "log-shipper")}:
        return False
    paths = {"NOMAD_TOKEN_FILE": "token", "NOMAD_CACERT": "ca.pem", "NOMAD_CLIENT_CERT": "cert.pem", "NOMAD_CLIENT_KEY": "key.pem"}
    if name in paths:
        return value == "/run/luma-nomad-credential/" + paths[name]
    if name == "NOMAD_ADDR":
        return value in {"http://127.0.0.1:4646", "https://127.0.0.1:4646"}
    if name in {"LUMA_NOMAD_CREDENTIAL_WRAPPER", "LUMA_NOMAD_COMPAT"}:
        return value == "1"
    return name == "LUMA_NOMAD_TLS_GENERATION" and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _namespace(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise LumaError("secret delivery requires an exact Nomad namespace")
    return value


def variable_path(job: str, group: str, task: str) -> str:
    if any(not _NAME.fullmatch(name) for name in (job, group, task)):
        raise LumaError("secret delivery requires unambiguous job, group and task names")
    return "/".join(("nomad/jobs", job, group, task))


def _route(path: str, namespace: str, *, cas: int | None = None) -> str:
    query: dict[str, Any] = {"namespace": _namespace(namespace)}
    if cas is not None:
        query["cas"] = cas
    return "/v1/var/" + urllib.parse.quote(path, safe="/") + "?" + urllib.parse.urlencode(query)


def _status(exc: Exception) -> int:
    return int(getattr(exc.__cause__, "code", 0) or 0)


def read_variable(client: Any, path: str, namespace: str = "default") -> dict[str, Any] | None:
    try:
        value = client.request("GET", _route(path, namespace))
    except LumaError as exc:
        if _status(exc) == 404:
            return None
        raise
    if not isinstance(value, dict) or value.get("Path") != path or value.get("Namespace") != namespace or not isinstance(value.get("Items"), dict):
        raise LumaError("Nomad returned an invalid encrypted Variable")
    return value


def validate_workload_acl(client: Any) -> None:
    """Do not inherit unknown grants that can override implicit task isolation."""
    policies = client.request("GET", "/v1/acl/policies")
    if not isinstance(policies, list):
        raise LumaError("cannot verify workload ACL ownership before secret migration")
    if any(isinstance(policy, dict) and policy.get("JobACL") for policy in policies):
        raise LumaError("existing workload-bound ACL policies need explicit review before enabling task secret isolation")


def write_variable(client: Any, path: str, items: Mapping[str, str], *, previous: dict[str, Any] | None,
                   namespace: str = "default") -> dict[str, Any]:
    if sum(len(key.encode()) + len(value.encode()) for key, value in items.items()) > MAX_VARIABLE_BYTES:
        raise LumaError("task secrets exceed Nomad's 64 KiB Variable limit; no plaintext fallback was used")
    if previous is not None and previous.get("Items") == items:
        return previous
    value = client.request("PUT", _route(path, namespace, cas=int((previous or {}).get("ModifyIndex") or 0)),
                           {"Namespace": namespace, "Path": path, "Items": dict(items)})
    if not isinstance(value, dict) or value.get("Items") != items or not int(value.get("ModifyIndex") or 0):
        raise LumaError("Nomad did not confirm the encrypted Variable write")
    return value


def _refs(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_refs(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_refs(item) for item in value), set())
    return set(_REFERENCE.findall(value)) if isinstance(value, str) else set()


def _retained_keys(client: Any, job_id: str, group_name: str, task_name: str, namespace: str) -> set[str]:
    route = "/v1/job/" + urllib.parse.quote(job_id, safe="")
    query = "?namespace=" + urllib.parse.quote(namespace, safe="")
    try:
        versions = client.request("GET", route + "/versions" + query) or {}
        allocations = client.request("GET", route + "/allocations" + query) or []
    except LumaError as exc:
        if _status(exc) == 404:
            return set()
        raise
    jobs = list(versions.get("Versions") or [])
    for allocation in allocations:
        if allocation.get("ClientStatus") not in {"complete", "failed", "lost"}:
            try:
                detail = client.request("GET", "/v1/allocation/" + urllib.parse.quote(str(allocation["ID"]), safe="") + query)
            except LumaError as exc:
                if _status(exc) == 404:
                    continue
                raise
            jobs.append(detail.get("Job") or {})
    keep: set[str] = set()
    for job in jobs:
        for group in job.get("TaskGroups") or []:
            if group.get("Name") == group_name:
                for task in group.get("Tasks") or []:
                    if task.get("Name") == task_name:
                        keep.update(_refs(task))
    return keep


def _publish_items(client: Any, path: str, items: dict[str, str], *, namespace: str,
                   job_id: str, group: str, task: str) -> None:
    for attempt in range(4):
        previous = read_variable(client, path, namespace)
        merged = {**((previous or {}).get("Items") or {}), **items}
        if sum(len(k.encode()) + len(v.encode()) for k, v in merged.items()) > MAX_VARIABLE_BYTES:
            retained = _retained_keys(client, job_id, group, task, namespace) | set(items)
            merged = {key: value for key, value in merged.items() if key in retained}
        try:
            write_variable(client, path, merged, previous=previous, namespace=namespace)
            return
        except LumaError as exc:
            if _status(exc) != 409 or attempt == 3:
                raise
    raise LumaError("concurrent task secret update did not converge")


def protect_job(job: dict[str, Any], client: Any, *, preserve_runtime: bool = False) -> dict[str, Any]:
    """Populate task Variables before returning a submit-safe job definition."""
    from .secret_storage import context, opaque_id
    job_id = str(job.get("ID") or "")
    namespace = _namespace(str(job.get("Namespace") or "default"))
    was_protected = (job.get("Meta") or {}).get("luma.secrets.version") == "1"
    job["Meta"] = {**(job.get("Meta") or {}), "luma.secrets.version": "1"}
    for group in job.get("TaskGroups") or []:
        group_name = str(group.get("Name") or "")
        for task in group.get("Tasks") or []:
            task_name = str(task.get("Name") or "")
            path = variable_path(job_id, group_name, task_name)
            bindings: list[tuple[dict[str, Any] | list[Any], str | int, str]] = []
            interpolated: list[tuple[dict[str, Any] | list[Any], str | int, list[str]]] = []

            def bind(container: dict[str, Any] | list[Any], key: str | int, value: str) -> None:
                parts, start = [], 0
                for match in _INTERPOLATION.finditer(value):
                    expression = match[0]
                    if not (_RUNTIME.fullmatch(expression) or expression[2:-1] in (task.get("Env") or {})
                            or (was_protected and _REFERENCE.fullmatch(expression))):
                        continue
                    parts.extend((value[start:match.start()], expression))
                    start = match.end()
                parts.append(value[start:])
                for index in range(0, len(parts), 2):
                    parts[index] = parts[index].replace("$${", "${").replace("%%{", "%{")
                if len(parts) == 1:
                    bindings.append((container, key, parts[0]))
                    return
                # Preserve client-side Nomad interpolation, but never leave a
                # literal password/URL prefix around it in the job definition.
                for index in range(0, len(parts), 2):
                    if parts[index]:
                        bindings.append((parts, index, parts[index]))
                interpolated.append((container, key, parts))

            environment = task.get("Env") or {}
            for name, value in sorted(environment.items()):
                if not isinstance(value, str):
                    raise LumaError("Nomad environment values must be strings")
                if _public_transport(name, value, job_id, task_name) or (preserve_runtime and _RUNTIME.fullmatch(value)) or (was_protected and _REFERENCE.fullmatch(value)):
                    continue
                # Rendered secret values are data, not expressions. Only
                # renderer-generated pure runtime references are exempt above.
                bindings.append((environment, name, value))
            config = task.get("Config") or {}
            auth = config.get("auth") or {}
            for name, value in sorted(auth.items()):
                if isinstance(value, str) and not (was_protected and _REFERENCE.fullmatch(value)):
                    bindings.append((auth, name, value))
            # Arguments may contain hard-coded credentials even when no env
            # secret was referenced. Nomad interpolates them only on the client.
            for name in (() if job_id in {"luma-control", "traefik", "egress", "luma-observe"} else ("command", "args", "entrypoint")):
                value = config.get(name)
                if isinstance(value, str) and not (was_protected and _REFERENCE.fullmatch(value)):
                    bind(config, name, value)
                elif isinstance(value, list):
                    for index, item in enumerate(value):
                        if isinstance(item, str) and not (was_protected and _REFERENCE.fullmatch(item)):
                            bind(value, index, item)
            existing = [item for item in task.get("Secrets") or [] if item.get("Name") == SECRET_NAME]
            if existing and (len(existing) != 1 or existing[0].get("Provider") != "nomad" or existing[0].get("Path") != path
                             or (existing[0].get("Config") or {}).get("namespace", namespace) != namespace):
                raise LumaError("application secret binding does not match its task identity")
            if bindings:
                revision = "v" + opaque_id([value for _, _, value in bindings], context("task", namespace, path))
                # Values are JSON quoted because Nomad's native secret provider
                # parses env syntax. Quotes/newlines/$ remain literal data.
                items = {revision + "_" + str(index): json.dumps(value, ensure_ascii=False)
                         for index, (_, _, value) in enumerate(bindings)}
                _publish_items(client, path, items, namespace=namespace, job_id=job_id, group=group_name, task=task_name)
                for index, (container, name, _) in enumerate(bindings):
                    container[name] = "${secret.luma." + revision + "_" + str(index) + "}"
                for container, name, parts in interpolated:
                    container[name] = "".join(parts)
                task["Meta"] = {**(task.get("Meta") or {}), "luma.secrets.revision": revision}
            if bindings or existing:
                task["Secrets"] = [item for item in task.get("Secrets") or [] if item.get("Name") != SECRET_NAME] + [
                    {"Name": SECRET_NAME, "Provider": "nomad", "Path": path, "Config": {"namespace": namespace}},
                ]
                # Keep the built-in allocation identity inside Nomad. No shared
                # reader token or signed identity is exposed in the container.
                task["Identity"] = {"Name": "default", "Env": False, "File": False}
    return job


def task_environment(client: Any, job: Mapping[str, Any], group: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, str]:
    """Private control-plane use only; never use this to build an API response."""
    environment = copy.deepcopy(task.get("Env") or {})
    if not _refs(environment):
        return environment
    path = variable_path(str(job["ID"]), str(group["Name"]), str(task["Name"]))
    variable = read_variable(client, path, str(job.get("Namespace") or "default"))
    items = (variable or {}).get("Items") or {}
    for name, value in environment.items():
        match = _REFERENCE.fullmatch(value)
        if match:
            try:
                environment[name] = json.loads(items[match[1]])
            except (ValueError, KeyError, TypeError):
                raise LumaError("a referenced application secret is unavailable") from None
    return environment


def verify_bindings(client: Any, job: Mapping[str, Any]) -> None:
    """Catch missing/stale variable generations before replacing a healthy job."""
    for group in job.get("TaskGroups") or []:
        for task in group.get("Tasks") or []:
            references = _refs(task)
            if not references:
                continue
            path = variable_path(str(job["ID"]), str(group["Name"]), str(task["Name"]))
            variable = read_variable(client, path, str(job.get("Namespace") or "default"))
            items = (variable or {}).get("Items") or {}
            try:
                if any(not isinstance(json.loads(items[key]), str) for key in references):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise LumaError("application secret generation is unavailable; repeat the normal deployment") from None


def prune_job_variables(client: Any, job: Mapping[str, Any]) -> None:
    """Retire unused values only after the submitted job is healthy."""
    job_id = str(job["ID"])
    namespace = str(job.get("Namespace") or "default")
    current = client.request("GET", "/v1/job/" + urllib.parse.quote(job_id, safe="") + "?namespace=" + urllib.parse.quote(namespace, safe=""))
    prefix = "nomad/jobs/" + job_id + "/"
    entries = client.request("GET", "/v1/vars?" + urllib.parse.urlencode({"namespace": namespace, "prefix": prefix})) or []
    for entry in entries:
        path = str(entry.get("Path") or "")
        if not path.startswith(prefix):
            raise LumaError("Nomad returned a Variable outside the requested application")
        names = path[len(prefix):].split("/")
        if len(names) != 2:
            raise LumaError("unexpected shared application Variable; no data was deleted")
        previous = read_variable(client, path, namespace)
        if previous is None:
            continue
        # A later write may belong to another deployment that has staged its
        # values but not submitted its job yet. Leave that generation alone.
        if int(previous.get("ModifyTime") or 0) > int(current.get("SubmitTime") or 0):
            continue
        keep = _retained_keys(client, job_id, names[0], names[1], namespace)
        items = {key: value for key, value in previous["Items"].items() if key in keep}
        if items == previous["Items"]:
            continue
        try:
            if items:
                write_variable(client, path, items, previous=previous, namespace=namespace)
            else:
                client.request("DELETE", _route(path, namespace, cas=int(previous["ModifyIndex"])))
        except LumaError as exc:
            if _status(exc) != 409:
                raise


def assert_protected_job(job: Mapping[str, Any]) -> None:
    if (job.get("Meta") or {}).get("luma.secrets.version") != "1" or not job.get("TaskGroups"):
        raise LumaError("Nomad job predates task-bound encrypted secret delivery")
    for group in job.get("TaskGroups") or []:
        for task in group.get("Tasks") or []:
            expected = variable_path(str(job.get("ID") or ""), str(group.get("Name") or ""), str(task.get("Name") or ""))
            definitions = task.get("Secrets") or []
            if any(item.get("Name") != SECRET_NAME or item.get("Provider") != "nomad" for item in definitions):
                raise LumaError("custom task secret providers require an explicit migration")
            if _refs(task) or definitions:
                bindings = [item for item in definitions if item.get("Name") == SECRET_NAME]
                if len(bindings) != 1 or bindings[0].get("Provider") != "nomad" or bindings[0].get("Path") != expected:
                    raise LumaError("task secrets are not bound to the correct application identity")
                if (bindings[0].get("Config") or {}).get("namespace", job.get("Namespace") or "default") != (job.get("Namespace") or "default"):
                    raise LumaError("task secret namespace differs from its allocation identity")
            identities = [task.get("Identity") or {}, *(task.get("Identities") or [])]
            if any(identity.get("Env") or identity.get("File") for identity in identities):
                raise LumaError("the internal Nomad workload identity must not be exposed to applications")
            for key, value in (task.get("Env") or {}).items():
                if not isinstance(value, str) or (not _public_transport(key, value, str(job["ID"]), str(task["Name"])) and not (_REFERENCE.fullmatch(value) or _RUNTIME.fullmatch(value))):
                    raise LumaError("Nomad job still contains inline environment values; render it through Luma")
            for value in ((task.get("Config") or {}).get("auth") or {}).values():
                if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
                    raise LumaError("Nomad job still contains inline registry credentials")
            if job["ID"] not in {"luma-control", "traefik", "egress", "luma-observe"}:
                config = task.get("Config") or {}
                for field in ("command", "args", "entrypoint"):
                    value = config.get(field)
                    for part in value if isinstance(value, list) else [value] if value is not None else []:
                        if not isinstance(part, str):
                            raise LumaError("invalid protected task command")
                        def replace(match):
                            expression = match[0]
                            return "" if (_REFERENCE.fullmatch(expression) or _RUNTIME.fullmatch(expression) or expression[2:-1] in (task.get("Env") or {})) else expression
                        if _INTERPOLATION.sub(replace, part):
                            raise LumaError("Nomad job still contains inline command values; render it through Luma")


def remove_job_variables(client: Any, job_id: str, *, before: int, namespace: str = "default") -> None:
    """Called under Control's deployment lock after purging this application."""
    try:
        client.request("GET", "/v1/job/" + urllib.parse.quote(job_id, safe="") + "?namespace=" + _namespace(namespace))
        return  # Another writer has recreated the job; retain its credentials.
    except LumaError as exc:
        if _status(exc) != 404:
            raise
    prefix = "nomad/jobs/" + job_id + "/"
    entries = client.request("GET", "/v1/vars?" + urllib.parse.urlencode({"namespace": namespace, "prefix": prefix})) or []
    for entry in entries:
        path = str(entry.get("Path") or "")
        if not path.startswith(prefix) or len(path[len(prefix):].split("/")) != 2:
            raise LumaError("unexpected application Variable path; no foreign data was deleted")
        previous = read_variable(client, path, namespace)
        if previous is None or int(previous.get("ModifyTime") or 0) > before:
            continue
        try:
            client.request("DELETE", _route(path, namespace, cas=int(previous["ModifyIndex"])))
        except LumaError as exc:
            if _status(exc) not in {404, 409}:
                raise
