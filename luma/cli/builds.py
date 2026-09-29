"""Luma CLI: builds commands."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict
from ..control.client import ControlClient
from ..errors import ControlRequestError, LumaError
from ..service import slugify
from . import common
from .common import _control_context, _history_query, _import_env_secrets, _output_format, _print_deploy_step, _print_history_expiry, _print_history_page, _print_json, _print_success, _print_table, _quiet, _run_with_wait_heartbeat


def _workflow_prepare(args: argparse.Namespace, client: ControlClient, *, name: str = "", repo_url: str = "") -> None:
    from ..deploy_workflow import make_recipe, project_root
    from ..local_build import _git_output

    if getattr(args, "skip_orchestrator", False):
        return
    if name and getattr(args, "workflow_app", "") and slugify(args.workflow_app) != slugify(name):
        raise LumaError(f"--workflow-app must match the manifest application name: {name}")
    recipe = make_recipe(args)
    if not repo_url and args.command != "import":
        start = getattr(args, "path", None) or getattr(args, "service", None) or getattr(args, "sidecar", None) or Path.cwd()
        repo_url = _git_output(project_root(start), "remote", "get-url", "origin")
    body: Dict[str, Any] = {
        "recipe": recipe,
        "selector": {"name": getattr(args, "workflow_app", "") or name, "repoUrl": repo_url},
    }
    checked = client.check_workflow(body)
    if not isinstance(checked, dict) or checked.get("status") not in {"unrecorded", "match", "confirmation-required"}:
        raise LumaError("Control returned an invalid workflow check; deployment was not started")
    previous = checked.get("workflow") or {}
    body["expectedRevision"] = previous.get("revision", 0)
    if previous and not body["selector"]["name"]:
        body["selector"]["name"] = previous["name"]
    if checked["status"] == "confirmation-required":
        differences = checked.get("differences") or []
        detail = "\n".join(
            f"  {row['field']}: {row.get('previous')!r} -> {row.get('requested')!r}"
            for row in differences
        )
        message = f"Deployment workflow differs from the recorded workflow for {previous.get('name', name)}:\n{detail}"
        approved = bool(getattr(args, "accept_workflow_change", False))
        if not approved and sys.stdin.isatty() and _output_format(args) == "text" and not _quiet(args):
            print(message, file=sys.stderr, flush=True)
            try:
                approved = input("Confirm this workflow change and deploy? [y/N] ").strip().lower() in {"y", "yes"}
            except EOFError:
                approved = False
        if not approved:
            raise LumaError(message + "\nDeployment was not started. Ask the user to confirm these changes, then rerun with --accept-workflow-change.")
        if _output_format(args) == "ndjson":
            _print_json({"type": "event", "status": "ok", "name": "Confirm workflow change", "differences": differences})
        elif _output_format(args) == "text":
            print(f"[ok] Workflow change confirmed: {previous.get('name', name)}", flush=True)
    elif _output_format(args) == "text" and not _quiet(args):
        print("[ok] Workflow matches the saved deployment" if previous else "[ok] No workflow recorded; a successful deployment will create it", flush=True)
    args._deployment_workflow = body
    client._queued_workflow = {**body, "note": getattr(args, "workflow_note", None)}


def _wait_for_queued_build(args: argparse.Namespace, client: ControlClient, result: Dict[str, Any]) -> Dict[str, Any]:
    """Wait for a durable server job; timeout/disconnect never cancels it."""
    if result.get("queued") is not True:
        return result
    build_id = str(result.get("buildRunId") or "")
    if not build_id:
        raise LumaError("Control accepted queued work without a build run ID")
    deadline = time.monotonic() + args.timeout
    seen = 0
    resume_after: int | None = None
    page_cursor: str | None = None
    failures = 0
    last_state = None

    def emit(event: Dict[str, Any]) -> None:
        if _output_format(args) == "ndjson":
            _print_json({"type": "event", **event})
        elif not _quiet(args):
            _print_deploy_step(event)

    def timeout_error() -> LumaError:
        return LumaError(f"Stopped waiting for {build_id}; the server task continues. Use luma build logs {build_id} or luma build cancel {build_id}")

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise timeout_error()
        query: Dict[str, Any] = {"limit": 100}
        if page_cursor:
            query["cursor"] = page_cursor
        elif resume_after is not None:
            query["after"] = resume_after
        try:
            detail = client.get_build(build_id, query=query, timeout=min(30, remaining))
        except ControlRequestError as exc:
            failures += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timeout_error() from exc
            delay = min(2 ** min(failures - 1, 4), remaining)
            emit({"name": "Project deployment queue", "status": "reconnecting",
                  "message": f"Connection interrupted; continuing to wait for {build_id} in {delay:g}s: {exc}",
                  "buildRunId": build_id, "retryInSeconds": delay})
            time.sleep(delay)
            continue
        failures = 0
        run = detail.get("run") or {}
        state = (run.get("status"), run.get("queuePosition"), run.get("waitingFor"), run.get("waitReason"), run.get("waitingForNode"))
        if state != last_state:
            message = f"Build {build_id}: {state[0]}"
            if state[0] == "queued":
                message += f"; queue position {state[1] or 1}"
                if state[2]:
                    message += f"; waiting for {state[2]}"
                reasons = {"target": "same branch/target is still active", "builder": "builder is busy",
                           "builder-offline": "builder is offline", "capacity": "Control execution slots are full"}
                if state[3] in reasons:
                    message += "; " + reasons[state[3]]
                if state[4]:
                    message += f" ({state[4]})"
            emit({"name": "Project deployment queue", "status": "start", "message": message, "buildRunId": build_id})
            last_state = state
        page = detail.get("eventsPage") or {}
        events = list(run.get("events") or [])
        if isinstance(page.get("resumeAfter"), int):
            for event in events:
                emit(event)
            resume_after = page["resumeAfter"]
            page_cursor = page.get("nextCursor")
            if page_cursor:
                continue
            # A terminal state can become visible while draining a frozen page.
            # Fetch once past its last event before returning, so final output is not lost.
            if events and state[0] in {"succeeded", "failed", "canceled"} and not run.get("queueExecuting"):
                continue
        else:
            # Older Control versions provide frozen pagination, but no live cursor.
            cursor = page.get("nextCursor")
            try:
                while cursor:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise timeout_error()
                    detail_page = client.get_build(build_id, query={"limit": 100, "cursor": cursor}, timeout=min(30, remaining))
                    events.extend((detail_page.get("run") or {}).get("events") or [])
                    cursor = (detail_page.get("eventsPage") or {}).get("nextCursor")
            except ControlRequestError as exc:
                emit({"name": "Project deployment queue", "status": "reconnecting",
                      "message": f"Connection interrupted; continuing to wait for {build_id}: {exc}", "buildRunId": build_id})
                time.sleep(min(2, max(0, deadline - time.monotonic())))
                continue
            for event in events[seen:]:
                emit(event)
            seen = len(events)
        if state[0] == "succeeded" and not run.get("queueExecuting"):
            return {**(run.get("result") or {}), "buildRunId": build_id}
        if state[0] in {"failed", "canceled"} and not run.get("queueExecuting"):
            raise LumaError(f"Build {build_id} {state[0]}: {run.get('message') or 'see build history'}")
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def _workflow_finish(args: argparse.Namespace, client: ControlClient, result: Dict[str, Any], *, name: str = "") -> None:
    recorded = result.get("workflow")
    if isinstance(recorded, dict) and isinstance(recorded.get("saved"), bool):
        if not recorded["saved"]:
            print(f"Warning: deployment succeeded, but workflow recording failed: {recorded.get('warning') or 'unknown error'}", file=sys.stderr)
        elif _output_format(args) == "text" and not _quiet(args):
            print("[ok] Workflow saved on Control", flush=True)
        return
    body = getattr(args, "_deployment_workflow", None)
    if not body:
        return
    name = result.get("service") or result.get("deployment") or name
    try:
        if not isinstance(name, str) or not name:
            raise LumaError("deploy response did not identify an application")
        image = result.get("image")
        if isinstance(image, dict):
            image = image.get("selected") or image.get("requested")
        evidence = {"image": image, "buildId": result.get("buildRunId") or result.get("buildId"), "revision": result.get("revision")}
        saved = client.record_workflow({
            **body, "name": name, "source": "cli-success", "evidence": evidence,
            "note": getattr(args, "workflow_note", None),
        })
        if not isinstance(saved, dict) or not isinstance(saved.get("workflow"), dict):
            raise LumaError("Control did not confirm saving the deployment workflow")
        result["workflow"] = {"saved": True, "name": name}
        if _output_format(args) == "text" and not _quiet(args):
            print(f"[ok] Workflow saved on Control: {name}", flush=True)
    except LumaError as exc:
        # The deployment has already succeeded: never report it as failed or
        # invite a blind redeploy just because recording failed afterwards.
        result["workflow"] = {"saved": False, "warning": str(exc)}
        if _output_format(args) == "text":
            print(f"[warn] Deployment succeeded, but workflow was not saved: {exc}", file=sys.stderr)


def cmd_workflow(args: argparse.Namespace) -> int:
    from ..deploy_workflow import describe_workflow, make_recipe, parse_recipe, project_root, replay_argv, validate_recipe

    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    if args.workflow_command == "list":
        result = client.list_workflows()
        if _output_format(args) == "text":
            for row in result.get("workflows") or []:
                print(f"{row['name']}\t{row['recipe']['method']}")
        else:
            _print_success(args, result)
        return 0
    if args.workflow_command == "record":
        argv = list(args.recipe_command)
        if argv and argv[0] == "--":
            argv.pop(0)
        if argv and argv[0] == "luma":
            argv.pop(0)
        deployment_args = parse_recipe(argv)
        if deployment_args.command == "build" and deployment_args.build_command == "retry":
            deployment_args._workflow_retry_run = client.get_build(deployment_args.id).get("run")
        recipe = make_recipe(deployment_args)
        from ..local_build import _git_output

        result = client.record_workflow({
            "name": args.name, "recipe": recipe, "source": "manual", "note": args.workflow_note,
            "selector": {"repoUrl": _git_output(project_root(Path.cwd()), "remote", "get-url", "origin")},
        })
    else:
        result = client.get_workflow(args.name)
    if args.workflow_command != "run":
        if _output_format(args) == "text":
            print(describe_workflow(result["workflow"]))
        else:
            _print_success(args, result)
        return 0
    recipe = validate_recipe(result["workflow"]["recipe"])
    argv = replay_argv(recipe["argv"])
    argv += ["--control-url", endpoint, "--token", token, "--workflow-app", args.name, "--format", _output_format(args)]
    if insecure:
        argv.append("--insecure")
    if resolve_ip:
        argv += ["--resolve-ip", resolve_ip]
    if args.accept_workflow_change:
        argv.append("--accept-workflow-change")
    if args.workflow_note is not None:
        argv += ["--workflow-note", args.workflow_note]
    if _quiet(args):
        argv.append("--quiet")
    root = project_root(args.path)
    if not root.is_dir():
        raise LumaError(f"project checkout does not exist: {root}")
    previous_cwd = Path.cwd()
    from .main import main

    try:
        os.chdir(root)
        return main(argv)
    finally:
        os.chdir(previous_cwd)


def cmd_import(args: argparse.Namespace) -> int:
    output_format = _output_format(args)
    quiet = _quiet(args) or output_format != "text"
    if args.timeout < 1:
        raise LumaError("--timeout must be at least 1 second")
    if not args.repo and not (args.provider_id and args.repository):
        raise LumaError("repo is required unless --provider-id and --repository are provided")
    if args.repo and (args.provider_id or args.repository):
        raise LumaError("use either repo URL/owner-name or --provider-id + --repository, not both")
    if args.compose_sidecar and args.manifest:
        raise LumaError("--compose-sidecar cannot be combined with --manifest")

    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    source_label = args.repo or f"{args.provider_id}:{args.repository}"
    build_node_label = args.build_node or "control default"
    if not quiet:
        print(f"[ok] Control endpoint: {endpoint}", flush=True)
        print(f"[start] Import: {source_label} (build on {build_node_label})", flush=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    _workflow_prepare(args, client)
    manifest_text = args.manifest.read_text(encoding="utf-8") if args.manifest else ""
    env_secrets = _import_env_secrets(args.deploy_env_file)

    build_kwargs: Dict[str, Any] = dict(
        repo_url=args.repo,
        provider_id=args.provider_id,
        repository=args.repository,
        build_node=args.build_node,
        ref=args.ref,
        region=args.region,
        exposure=args.exposure,
        domain=args.domain,
        port=args.port,
        manifest=manifest_text,
        compose_sidecar=args.compose_sidecar,
        env_secrets=env_secrets,
        platform=args.platform,
        context=args.build_context,
        dockerfile=args.dockerfile,
        registry_host=args.registry_host,
        proxy_mode=args.proxy_mode,
        timeout=args.timeout,
    )

    streamed = False
    result: Dict[str, Any] | None = None
    try:
        for event in client.build_deploy_events(**build_kwargs):
            status = str(event.get("status") or "")
            if output_format == "ndjson":
                accepted = status == "done" and isinstance(event.get("result"), dict) and event["result"].get("queued") is True
                _print_json({"type": "event", **event, **({"status": "queued"} if accepted else {})})
            if status in {"start", "ok", "fail"}:
                if not quiet:
                    _print_deploy_step(event)
                if status == "fail":
                    raise LumaError(str(event.get("message") or "import failed"))
            elif status == "done":
                payload = event.get("result")
                if not isinstance(payload, dict):
                    raise LumaError("control API stream ended without an import result")
                result = payload
            streamed = True
    except LumaError as exc:
        if "control API error 404" not in str(exc):
            raise

    if result is None:
        if streamed:
            raise LumaError("control API stream ended without an import result")
        if not quiet:
            print(f"[start] Waiting for control plane response (timeout {args.timeout}s)", flush=True)
        result = _run_with_wait_heartbeat(
            lambda: client.build_deploy(**build_kwargs),
            timeout=args.timeout,
            emit=not quiet,
        )
        for step in result.get("steps") or []:
            if isinstance(step, dict):
                if output_format == "ndjson":
                    _print_json({"type": "event", **step})
                elif not quiet:
                    _print_deploy_step(step)

    result = _wait_for_queued_build(args, client, result)
    if args.compose_sidecar and result.get("composeSidecar") != args.compose_sidecar:
        raise LumaError(
            "Control did not confirm the selected Compose sidecar; refusing to report import success"
        )

    _workflow_finish(args, client, result)
    if output_format != "text":
        _print_success(args, result)
        return 0
    print(f"[ok] Import finished: {result.get('service') or result.get('deployment') or source_label}")
    if result.get("image"):
        image = result["image"]
        if isinstance(image, str):
            print(f"Image built: {image}")
        elif isinstance(image, dict):
            print(f"Image ready: {image.get('selected') or image.get('requested')}")
    if result.get("dns"):
        print(result["dns"])
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    output_format = _output_format(args)
    if args.build_command == "local":
        from ..local_build import (
            build_and_push_local_source,
            local_deployment_target,
            local_source_metadata,
        )

        quiet = _quiet(args) or output_format != "text"
        if args.timeout < 1:
            raise LumaError("--timeout must be at least 1 second")
        metadata = local_source_metadata(args.path, repo_url=args.repo_url)
        from ..agent import _find_luma_deployment_manifest, _select_luma_compose_manifest
        from ..io import load_yaml

        selected = ("compose", _select_luma_compose_manifest(Path(metadata["path"]), args.compose_sidecar)) if args.compose_sidecar else _find_luma_deployment_manifest(Path(metadata["path"]))
        workflow_name = str(load_yaml(selected[1]).get("name") or "") if selected else ""
        _workflow_prepare(args, client, name=workflow_name, repo_url=metadata["repoUrl"])
        target = local_deployment_target(
            args.path,
            compose_sidecar=args.compose_sidecar,
            region=args.region,
        )
        prepare_body: Dict[str, Any] = {
            "repoUrl": metadata["repoUrl"],
            "sourceRevision": metadata["revision"],
            "ref": metadata.get("ref", ""),
            **target,
        }
        for key, value in {
            "region": args.region,
            "exposure": args.exposure,
            "domain": args.domain,
            "port": args.port,
            "platform": args.platform,
            "context": args.build_context,
            "dockerfile": args.dockerfile,
            "composeSidecar": args.compose_sidecar,
        }.items():
            if value not in (None, ""):
                prepare_body[key] = value
        prepared = client.prepare_local_build(prepare_body)
        run = prepared.get("run") if isinstance(prepared.get("run"), dict) else {}
        upload = prepared.get("upload") if isinstance(prepared.get("upload"), dict) else {}
        build_id = str(run.get("id") or "")
        if not build_id:
            raise LumaError("control API did not return a local build reservation")
        if not quiet:
            print(f"[ok] Project reserved: {upload.get('repository')} ({build_id})", flush=True)
            print(f"[start] Building locally and pushing to {upload.get('registryHost')}", flush=True)
        try:
            build_result = build_and_push_local_source(
                Path(metadata["path"]),
                registry_host=str(upload.get("registryHost") or ""),
                repository=str(upload.get("repository") or ""),
                tag=str(upload.get("tag") or ""),
                compose_sidecar=args.compose_sidecar,
                context=args.build_context,
                dockerfile=args.dockerfile,
                platform=str(upload.get("platform") or ""),
                builder=args.builder,
                proxy=args.proxy,
                timeout=args.timeout,
                progress=(lambda line: print(line, flush=True)) if not quiet else None,
            )
            result = client.complete_local_build(
                build_id,
                build_result=build_result,
                env_secrets=_import_env_secrets(args.deploy_env_file),
                timeout=args.timeout,
            )
        except (Exception, KeyboardInterrupt) as exc:
            try:
                client.fail_local_build(build_id, str(exc))
            except Exception:
                pass
            raise
        result = _wait_for_queued_build(args, client, result)
        _workflow_finish(args, client, result, name=workflow_name)
        if output_format != "text":
            _print_success(args, result)
            return 0
        print(f"[ok] Local build deployed: {result.get('service') or result.get('deployment') or upload.get('repository')}")
        print(f"Image: {result.get('image') or build_result.get('image')}")
        print(f"Build run: {build_id}")
        return 0
    if args.build_command == "list":
        result = client.list_builds(query=_history_query(args))
        if output_format != "text":
            _print_success(args, result)
            return 0
        rows = []
        for run in result.get("runs") or []:
            if not isinstance(run, dict):
                continue
            rows.append(
                [
                    str(run.get("id") or ""),
                    str(run.get("status") or ""),
                    str(run.get("buildNode") or ""),
                    str(run.get("providerId") or ""),
                    str(run.get("repository") or run.get("source") or ""),
                    str(run.get("ref") or "-"),
                    str(run.get("message") or "")[:80],
                ]
            )
        if rows:
            _print_table(["ID", "STATUS", "NODE", "PROVIDER", "REPOSITORY/SOURCE", "REF", "MESSAGE"], rows)
        else:
            print("No build runs recorded")
        _print_history_page(result.get("page"))
        return 0
    if args.build_command == "logs":
        result = client.get_build(args.id, query=_history_query(args, detail=True))
        if output_format != "text":
            _print_success(args, result)
            return 0
        run = result.get("run") if isinstance(result.get("run"), dict) else {}
        print(f"Build run: {run.get('id') or args.id}")
        print(f"Status: {run.get('status') or '-'}")
        print(f"Source: {run.get('source') or '-'}")
        _print_history_expiry(run)
        for event in run.get("events") or []:
            if isinstance(event, dict):
                _print_deploy_step(event)
        _print_history_page(result.get("eventsPage"))
        return 0
    if args.build_command == "retry":
        args._workflow_retry_run = client.get_build(args.id).get("run")
        prior_result = (args._workflow_retry_run or {}).get("result") or {}
        _workflow_prepare(args, client, name=str(prior_result.get("service") or prior_result.get("deployment") or ""))
        env_secrets = _import_env_secrets(args.deploy_env_file)
        result = client.retry_build(args.id, timeout=args.timeout, env_secrets=env_secrets)
        result = _wait_for_queued_build(args, client, result)
        _workflow_finish(args, client, result)
        if output_format != "text":
            _print_success(args, result)
            return 0
        print(f"[ok] Build retry finished: {result.get('service') or result.get('deployment') or args.id}")
        if result.get("buildRunId"):
            print(f"Build run: {result.get('buildRunId')}")
        if result.get("image"):
            print(f"Image built: {result.get('image')}")
        return 0
    if args.build_command == "cancel":
        result = client.cancel_build(args.id)
        if output_format != "text":
            _print_success(args, result)
            return 0
        run = result.get("run") if isinstance(result.get("run"), dict) else {}
        print(f"Build {run.get('id') or args.id}: {run.get('status') or 'canceling'}")
        return 0
    if args.build_command == "config":
        nodes = [str(value).strip() for value in args.nodes if str(value).strip()]
        default_node = str(args.default_node or (nodes[0] if nodes else "")).strip()
        direct_egress_nodes = [] if args.clear_direct_egress else args.direct_egress_nodes
        if args.clear_direct_egress and args.direct_egress_nodes:
            raise LumaError("--clear-direct-egress cannot be combined with --direct-egress-node")
        if not nodes and not default_node and not args.registry_host and not args.push_host and direct_egress_nodes is None:
            raise LumaError("build config requires a builder, registry, or direct-egress option")
        result = client.configure_build(
            nodes=nodes or None,
            default_node=default_node,
            registry_host=args.registry_host,
            push_host=args.push_host,
            direct_egress_nodes=direct_egress_nodes,
        )
        if output_format != "text":
            _print_success(args, result)
            return 0
        build = result.get("build") if isinstance(result.get("build"), dict) else {}
        print("Build config saved")
        print(f"  Default node: {build.get('defaultNode') or '-'}")
        print(f"  Registry host: {build.get('registryHost') or '-'}")
        print(f"  Push host: {build.get('pushHost') or '-'}")
        print(f"  Direct egress: {', '.join(build.get('directEgressNodes') or []) or '-'}")
        rows = []
        for node in build.get("nodes") or []:
            if isinstance(node, dict):
                rows.append([
                    str(node.get("name") or ""),
                    str(node.get("region") or ""),
                    "yes" if node.get("ready") else "no",
                    ",".join(str(value) for value in node.get("storageCapabilities") or []) or "-",
                ])
        if rows:
            _print_table(["NODE", "REGION", "READY", "CAPABILITIES"], rows)
        return 0
    raise LumaError(f"unknown build command: {args.build_command}")
