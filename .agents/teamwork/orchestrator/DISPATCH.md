# Dispatch History

## 2026-10-15T16:01:45Z
You are the Project Orchestrator for this task.

Working directory: /Users/turning4th/infra-stacks/.agents/teamwork/orchestrator
Project workspace root: /Users/turning4th/infra-stacks

Your authoritative task requirements are recorded in:
/Users/turning4th/infra-stacks/.agents/teamwork/ORIGINAL_REQUEST.md

Read ORIGINAL_REQUEST.md immediately. Maintain your BRIEFING.md and progress.md in your working directory.
Decompose the work, dispatch specialists, monitor execution, ensure all code and documentation fixes are applied and all automated tests (including `check-luma.sh`, unit tests, and `scripts/check-site.py`) pass.

When you have achieved completion and verified all acceptance criteria, send your victory report to me (the Sentinel).

## 2026-10-15T18:12:28Z

URGENT USER FEEDBACK & REQUIREMENTS UPDATE:
1. Migration is a ONE-TIME operation:
   - Provide a standalone, self-contained one-time migration script (`scripts/migrate-nomad-to-k3s.py`).
   - In `luma/cli/parser.py`, hide `migrate` from public `COMMAND_GROUPS` / top-level `--help` (using `argparse.SUPPRESS` or keeping it out of `COMMAND_GROUPS` and regenerated `docs/luma-cli-reference*.md`) so new open-source users do not see one-time legacy Nomad migration commands cluttering `luma --help`.
   - In `luma/paas/migration/migrator.py` and `docs/migration-guide*.md`, address real-world hazards:
     - Same-node coexistence & port conflicts: Edge Traefik (`80`/`443` and `tcp-relay` ports) must NOT conflict during migration; check memory headroom.
     - Stateful SQLite / flock volumes (`kato`, `bot`) & single-instance bots: NEVER simultaneously mount and write the same hostPath volume from both Nomad and K3s (corrupts SQLite WAL/flock) or run dual bot long-polling! `shadow` must keep `replicas: 0`, and `cutover` must stop Nomad allocation BEFORE scaling K3s to 1.
     - Post-migration cleanup: Document and script the final one-time Nomad decommission step (`systemctl disable --now nomad`).
2. Open-Source Developer Experience (0-to-1 Setup, `luma bootstrap`, Cloudflare & `.env` 100% Compatibility):
   - Zero breaking changes to 0-to-1 setup flow (`luma bootstrap --domain ...`, `luma login`, `luma init`, `luma node join`, `luma manager cloudflare --zone ...`, `luma manager egress`, `.env` settings `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID`, etc.).
   - Ensure Cloudflare DNS & Cloudflare Tunnel integration in `luma/cloudflare.py` and `luma/paas/release/helm_compiler.py` works seamlessly with Luma's existing Cloudflare configuration (`luma manager cloudflare` / `CLOUDFLARE_API_TOKEN`).
   - Document the 0-to-1 fresh install experience vs. existing user compatibility in docs.
