from __future__ import annotations

import argparse
import ipaddress
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
EXPECTED_SCHEMAS = {
    "build-plan-candidate.v1.schema.json",
    "build-plan-proposal.v1.schema.json",
    "build-plan.v1.schema.json",
    "deployment-plan.v1.schema.json",
    "error.v1.schema.json",
    "luma-builder-task.v1.schema.json",
    "operation-event.v1.schema.json",
}

_BUILD_PLAN_SCHEMAS = {
    "build-plan-candidate.v1.schema.json",
    "build-plan-proposal.v1.schema.json",
    "build-plan.v1.schema.json",
}
_IMAGE_COMPONENT = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")
_IMAGE_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PRIVATE_REGISTRY_SUFFIXES = (
    ".corp",
    ".home",
    ".internal",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
    ".private",
)


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str


class ContractRepositoryError(RuntimeError):
    def __init__(self, issues: Iterable[ValidationIssue]):
        self.issues = tuple(issues)
        super().__init__(
            "; ".join(f"{issue.path}: {issue.message}" for issue in self.issues)
        )


def specs_root() -> Path:
    return Path(__file__).resolve().parent / "specs"


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_schema(name: str) -> dict[str, Any]:
    if Path(name).name != name:
        raise ValueError("schema name must not contain a path")
    path = specs_root() / "schemas" / name
    value = _read_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"schema {name} must be a JSON object")
    return value


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _resolve_local_ref(root: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"only local JSON Pointer references are supported: {ref}")
    current: Any = root
    for raw_token in ref[2:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or token not in current:
            raise ValueError(f"unresolved JSON Pointer reference: {ref}")
        current = current[token]
    if not isinstance(current, Mapping):
        raise ValueError(f"JSON Pointer does not resolve to a schema object: {ref}")
    return current


def _resolve_ref(
    root: Mapping[str, Any], ref: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if ref.startswith("#/"):
        return _resolve_local_ref(root, ref), root
    filename, separator, fragment = ref.partition("#")
    if Path(filename).name != filename or not filename.endswith(".schema.json"):
        raise ValueError(f"unsupported external schema reference: {ref}")
    external_root = load_schema(filename)
    if not separator or not fragment:
        return external_root, external_root
    if not fragment.startswith("/"):
        raise ValueError(f"invalid external JSON Pointer reference: {ref}")
    return _resolve_local_ref(external_root, f"#{fragment}"), external_root


def _child_path(path: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key)}]"


def _validate(
    value: Any,
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    path: str,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    ref = schema.get("$ref")
    if isinstance(ref, str):
        try:
            target, target_root = _resolve_ref(root, ref)
        except ValueError as exc:
            return [ValidationIssue(path, str(exc))]
        issues.extend(_validate(value, target, target_root, path))

    for subschema in schema.get("allOf", []):
        issues.extend(_validate(value, subschema, root, path))

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        matches = sum(
            not _validate(value, candidate, root, path) for candidate in one_of
        )
        if matches != 1:
            issues.append(
                ValidationIssue(
                    path, f"must match exactly one oneOf branch; matched {matches}"
                )
            )

    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and not any(
        not _validate(value, candidate, root, path) for candidate in any_of
    ):
        issues.append(ValidationIssue(path, "must match at least one anyOf branch"))

    if "const" in schema and value != schema["const"]:
        issues.append(ValidationIssue(path, f"must equal constant {schema['const']!r}"))
    if "enum" in schema and value not in schema["enum"]:
        issues.append(ValidationIssue(path, f"must be one of {schema['enum']!r}"))

    expected_type = schema.get("type")
    if expected_type is not None:
        expected_types = (
            [expected_type] if isinstance(expected_type, str) else list(expected_type)
        )
        if not any(_json_type_matches(value, item) for item in expected_types):
            issues.append(
                ValidationIssue(path, f"must have JSON type {expected_types!r}")
            )
            return issues

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                issues.append(ValidationIssue(_child_path(path, key), "is required"))
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            for key, child in properties.items():
                if key in value:
                    issues.extend(
                        _validate(value[key], child, root, _child_path(path, key))
                    )
            if schema.get("additionalProperties") is False:
                for key in value.keys() - properties.keys():
                    issues.append(
                        ValidationIssue(
                            _child_path(path, key), "additional property is not allowed"
                        )
                    )
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            issues.append(
                ValidationIssue(
                    path, f"must contain at least {schema['minProperties']} properties"
                )
            )

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            issues.append(
                ValidationIssue(
                    path, f"must contain at least {schema['minItems']} items"
                )
            )
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            issues.append(
                ValidationIssue(
                    path, f"must contain at most {schema['maxItems']} items"
                )
            )
        if schema.get("uniqueItems"):
            encoded = [
                json.dumps(item, sort_keys=True, separators=(",", ":"))
                for item in value
            ]
            if len(set(encoded)) != len(encoded):
                issues.append(ValidationIssue(path, "must contain unique items"))
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                issues.extend(
                    _validate(item, item_schema, root, _child_path(path, index))
                )

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            issues.append(
                ValidationIssue(
                    path, f"must be at least {schema['minLength']} characters"
                )
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            issues.append(
                ValidationIssue(
                    path, f"must be at most {schema['maxLength']} characters"
                )
            )
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            issues.append(ValidationIssue(path, f"must match pattern {pattern!r}"))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            issues.append(ValidationIssue(path, f"must be >= {schema['minimum']}"))
        if "maximum" in schema and value > schema["maximum"]:
            issues.append(ValidationIssue(path, f"must be <= {schema['maximum']}"))

    return issues


def is_safe_external_image_reference(value: Any) -> bool:
    """Return whether a Docker image ref is safe for server-side resolution.

    LAE accepts immutable sha256 references and explicit, non-latest tags. An
    explicit registry must be a public DNS hostname on its default port;
    localhost, IP literals, single-label registries, and conventional private
    DNS suffixes are denied.
    """

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or value != value.strip()
        or any(character.isspace() for character in value)
        or "://" in value
        or "?" in value
        or "#" in value
        or "\\" in value
    ):
        return False

    if value.count("@") > 1:
        return False
    if "@" in value:
        name_with_tag, digest = value.rsplit("@", 1)
        if not _IMAGE_DIGEST.fullmatch(digest):
            return False
    else:
        name_with_tag = value
        digest = None

    last_slash = name_with_tag.rfind("/")
    last_component = name_with_tag[last_slash + 1 :]
    if ":" in last_component:
        if digest is not None:
            return False
        repository_component, tag = last_component.rsplit(":", 1)
        if not _IMAGE_TAG.fullmatch(tag) or tag.lower() == "latest":
            return False
        name = name_with_tag[: last_slash + 1] + repository_component
    else:
        if digest is None:
            return False
        name = name_with_tag

    if not name or name.startswith("/") or name.endswith("/") or "//" in name:
        return False
    components = name.split("/")
    registry: str | None = None
    if len(components) > 1 and (
        "." in components[0]
        or ":" in components[0]
        or components[0].lower() == "localhost"
    ):
        registry = components.pop(0)
    if not components or not all(
        _IMAGE_COMPONENT.fullmatch(item) for item in components
    ):
        return False
    if registry is None:
        return True

    if ":" in registry or "[" in registry or "]" in registry:
        return False
    if registry != registry.lower():
        return False
    host = registry
    if host == "localhost" or host.endswith(_PRIVATE_REGISTRY_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    labels = host.split(".")
    if len(labels) < 2 or not all(_DNS_LABEL.fullmatch(label) for label in labels):
        return False
    if labels[-1].isdigit():
        return False
    return True


def _validate_build_plan_semantics(value: Any) -> list[ValidationIssue]:
    if not isinstance(value, Mapping):
        return []
    issues: list[ValidationIssue] = []
    builds = value.get("builds")
    external_images = value.get("externalImages")
    build_keys = (
        [
            item.get("key")
            for item in builds
            if isinstance(builds, list) and isinstance(item, Mapping)
        ]
        if isinstance(builds, list)
        else []
    )
    external_keys = (
        [
            item.get("key")
            for item in external_images
            if isinstance(external_images, list) and isinstance(item, Mapping)
        ]
        if isinstance(external_images, list)
        else []
    )
    all_keys = [key for key in (*build_keys, *external_keys) if isinstance(key, str)]
    if isinstance(builds, list) and isinstance(external_images, list):
        if len(builds) + len(external_images) > 64:
            issues.append(
                ValidationIssue(
                    "$",
                    "builds and externalImages must contain at most 64 total items",
                )
            )
    if len(all_keys) != len(set(all_keys)):
        issues.append(
            ValidationIssue(
                "$.externalImages",
                "build and external image keys must be globally unique",
            )
        )
    if isinstance(external_images, list):
        for index, item in enumerate(external_images):
            if not isinstance(item, Mapping):
                continue
            ref = item.get("ref")
            if isinstance(ref, str) and not is_safe_external_image_reference(ref):
                issues.append(
                    ValidationIssue(
                        _child_path(_child_path("$", "externalImages"), index) + ".ref",
                        "must be an explicitly versioned image on a public registry",
                    )
                )
            resolved_digest = item.get("resolvedDigest")
            if (
                value.get("schemaVersion") == "lae.build-plan-proposal/v1"
                and isinstance(ref, str)
                and "@" not in ref
                and resolved_digest is not None
            ):
                issues.append(
                    ValidationIssue(
                        _child_path(_child_path("$", "externalImages"), index)
                        + ".resolvedDigest",
                        "must be omitted for tagged proposal references",
                    )
                )
            if (
                value.get("schemaVersion") == "lae.build-plan-proposal/v1"
                and isinstance(ref, str)
                and "@" in ref
                and resolved_digest is None
            ):
                issues.append(
                    ValidationIssue(
                        _child_path(_child_path("$", "externalImages"), index)
                        + ".resolvedDigest",
                        "is required for digest proposal references",
                    )
                )
            if (
                isinstance(ref, str)
                and "@" in ref
                and isinstance(resolved_digest, str)
                and resolved_digest != ref.rsplit("@", 1)[1]
            ):
                issues.append(
                    ValidationIssue(
                        _child_path(_child_path("$", "externalImages"), index)
                        + ".resolvedDigest",
                        "must equal the digest embedded in ref",
                    )
                )
    return issues


def validate_instance(schema_name: str, value: Any) -> list[ValidationIssue]:
    schema = load_schema(schema_name)
    issues = _validate(value, schema, schema, "$")
    if schema_name in _BUILD_PLAN_SCHEMAS:
        issues.extend(_validate_build_plan_semantics(value))
    elif schema_name == "luma-builder-task.v1.schema.json" and isinstance(
        value, Mapping
    ):
        payload = value.get("payload")
        signed_plan = (
            payload.get("signedBuildPlan") if isinstance(payload, Mapping) else None
        )
        for issue in _validate_build_plan_semantics(signed_plan):
            suffix = issue.path[1:] if issue.path.startswith("$") else issue.path
            issues.append(
                ValidationIssue(f"$.payload.signedBuildPlan{suffix}", issue.message)
            )
    return issues


def _safe_example_path(examples_root: Path, relative: str) -> Path:
    candidate = (examples_root / relative).resolve()
    if examples_root.resolve() not in candidate.parents:
        raise ValueError(f"example path escapes the examples directory: {relative}")
    return candidate


def validate_repository() -> dict[str, int | str]:
    root = specs_root()
    issues: list[ValidationIssue] = []
    schema_paths = sorted((root / "schemas").glob("*.schema.json"))
    names = {path.name for path in schema_paths}
    if names != EXPECTED_SCHEMAS:
        issues.append(
            ValidationIssue(
                "schemas",
                f"expected {sorted(EXPECTED_SCHEMAS)!r}, found {sorted(names)!r}",
            )
        )

    ids: set[str] = set()
    for path in schema_paths:
        try:
            schema = _read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(ValidationIssue(str(path), f"invalid JSON: {exc}"))
            continue
        if schema.get("$schema") != DRAFT_2020_12:
            issues.append(
                ValidationIssue(str(path), "must declare JSON Schema Draft 2020-12")
            )
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str) or not schema_id.startswith(
            "https://schemas.itool.tech/lae/"
        ):
            issues.append(ValidationIssue(str(path), "must have a stable LAE $id"))
        elif schema_id in ids:
            issues.append(
                ValidationIssue(str(path), f"duplicate schema $id {schema_id}")
            )
        else:
            ids.add(schema_id)
        if not isinstance(schema.get("title"), str):
            issues.append(ValidationIssue(str(path), "must have a title"))

    examples_root = root / "examples"
    try:
        manifest = _read_json(examples_root / "manifest.json")
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(ValidationIssue("examples/manifest.json", f"invalid JSON: {exc}"))
        manifest = {"suites": []}

    valid_count = 0
    invalid_count = 0
    covered: set[str] = set()
    for suite in manifest.get("suites", []):
        schema_name = suite.get("schema")
        if schema_name not in EXPECTED_SCHEMAS:
            issues.append(
                ValidationIssue(
                    "examples/manifest.json", f"unknown schema {schema_name!r}"
                )
            )
            continue
        covered.add(schema_name)
        for expected_valid, field in ((True, "valid"), (False, "invalid")):
            paths = suite.get(field, [])
            if not paths:
                issues.append(
                    ValidationIssue(
                        "examples/manifest.json",
                        f"{schema_name} has no {field} examples",
                    )
                )
            for relative in paths:
                try:
                    example_path = _safe_example_path(examples_root, relative)
                    value = _read_json(example_path)
                    validation = validate_instance(schema_name, value)
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    issues.append(
                        ValidationIssue(relative, f"cannot load example: {exc}")
                    )
                    continue
                if expected_valid:
                    valid_count += 1
                    issues.extend(
                        ValidationIssue(relative + issue.path[1:], issue.message)
                        for issue in validation
                    )
                else:
                    invalid_count += 1
                    if not validation:
                        issues.append(
                            ValidationIssue(
                                relative, "invalid example unexpectedly passed"
                            )
                        )
    if covered != EXPECTED_SCHEMAS:
        issues.append(
            ValidationIssue(
                "examples/manifest.json",
                f"example suites must cover every schema; covered {sorted(covered)!r}",
            )
        )

    try:
        catalog = _read_json(root / "events" / "event-catalog.v1.yaml")
        events = catalog["events"]
        catalog_types = [event["type"] for event in events]
        schema_types = load_schema("operation-event.v1.schema.json")["properties"][
            "type"
        ]["enum"]
        if len(catalog_types) != len(set(catalog_types)):
            issues.append(ValidationIssue("events", "event types must be unique"))
        if set(catalog_types) != set(schema_types):
            issues.append(
                ValidationIssue(
                    "events", "catalog and operation-event enum must match exactly"
                )
            )
        for index, event in enumerate(events):
            if set(event) != {"type", "phase", "terminal", "description"}:
                issues.append(
                    ValidationIssue(
                        f"events[{index}]", "event catalog entry has invalid fields"
                    )
                )
            if not isinstance(event.get("terminal"), bool):
                issues.append(
                    ValidationIssue(f"events[{index}].terminal", "must be boolean")
                )
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        issues.append(
            ValidationIssue("events/event-catalog.v1.yaml", f"invalid catalog: {exc}")
        )
        catalog_types = []

    if issues:
        raise ContractRepositoryError(issues)
    return {
        "status": "ok",
        "schemas": len(schema_paths),
        "validExamples": valid_count,
        "invalidExamples": invalid_count,
        "eventTypes": len(catalog_types),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lae-contracts")
    parser.add_argument("command", choices=("validate",), nargs="?", default="validate")
    parser.parse_args(argv)
    try:
        result = validate_repository()
    except ContractRepositoryError as exc:
        print(
            json.dumps(
                {"status": "failed", "issues": [asdict(issue) for issue in exc.issues]},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0
