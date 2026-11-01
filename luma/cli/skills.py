"""User-level skills paired with the running Luma CLI."""
from pathlib import Path

from ..skills import agent_roots, report_sync, skill_status, sync_skills
from .common import _output_format, _print_success


def cmd_skills(args) -> int:
    if args.skills_command == "status":
        result = skill_status()
    else:
        roots = None
        if args.skills_command == "install":
            defaults = agent_roots()
            roots = ([Path(args.path)] if args.path else list(defaults.values())
                     if args.agent == "all" else [defaults[args.agent]])
        result = sync_skills(roots, register=roots is not None, force=args.force)
        report_sync(result)
    if _output_format(args) != "text":
        _print_success(args, result)
    elif args.skills_command == "status":
        print(f"Luma {result['version']}")
        for item in result["items"]:
            print(f"{item['path']}: version={item['version'] or 'unmanaged'} paired={item['paired']} modified={item['modified']}")
    if not result["items"] and _output_format(args) == "text":
        print("No installed Luma skills found. Use 'luma skills install --agent codex' or '--agent claude'.")
    return int(bool(result.get("warnings")))
