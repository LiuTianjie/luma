from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from lae_agent_core import AnalysisError, analyze_source, canonical_bytes
from lae_contracts import validate_instance
from lae_core import VERSION, component_payload, emit_json


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise AnalysisError("LAE_ARGUMENT_INVALID", "Runner arguments are invalid")


def _run_envelope() -> int:
    try:
        envelope = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        emit_json(
            {
                "schemaVersion": "lae.agent-runner-envelope/v1",
                "status": "rejected",
                "errors": [{"path": "$", "message": f"invalid JSON: {exc.msg}"}],
            }
        )
        return 5
    issues = validate_instance("luma-builder-task.v1.schema.json", envelope)
    if issues:
        emit_json(
            {
                "schemaVersion": "lae.agent-runner-envelope/v1",
                "status": "rejected",
                "errors": [asdict(issue) for issue in issues],
            }
        )
        return 5
    emit_json(
        {
            "schemaVersion": "lae.agent-runner-envelope/v1",
            "externalOperationId": envelope["externalOperationId"],
            "kind": envelope["kind"],
            "status": "validated",
            "runnerVersion": VERSION,
            "executionImplemented": False,
        }
    )
    return 0


def _analyze(argv: Sequence[str]) -> int:
    parser = _SafeArgumentParser(prog="lae-agent-runner analyze")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        raw_metadata = args.metadata.read_text(encoding="utf-8")
        metadata = json.loads(raw_metadata)
        if not isinstance(metadata, dict):
            raise AnalysisError(
                "LAE_METADATA_INVALID", "Metadata must be a JSON object"
            )
        result = analyze_source(args.source, metadata, args.output_dir)
    except FileNotFoundError:
        _emit_error("LAE_INPUT_NOT_FOUND", "Source or metadata input was not found")
        return 2
    except (OSError, UnicodeDecodeError):
        _emit_error(
            "LAE_INPUT_UNREADABLE", "Source or metadata input could not be read"
        )
        return 2
    except json.JSONDecodeError:
        _emit_error("LAE_METADATA_INVALID", "Metadata is not valid JSON")
        return 2
    except AnalysisError as exc:
        _emit_error(exc.code, str(exc))
        return 2
    sys.stdout.write(canonical_bytes(result).decode("utf-8") + "\n")
    return 0


def _emit_error(code: str, message: str) -> None:
    payload = {
        "schemaVersion": "lae.agent-runner-error/v1",
        "code": code,
        "message": message,
    }
    sys.stderr.write(canonical_bytes(payload).decode("utf-8") + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "analyze":
        return _analyze(arguments[1:])
    parser = argparse.ArgumentParser(prog="lae-agent-runner")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--health", action="store_true")
    action.add_argument("--version", action="store_true")
    action.add_argument(
        "--run",
        action="store_true",
        help="Validate a builder task JSON envelope from stdin",
    )
    args = parser.parse_args(arguments)
    if args.run:
        return _run_envelope()
    if args.version:
        emit_json(component_payload("lae-agent-runner", status="version"))
        return 0
    emit_json(component_payload("lae-agent-runner"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
