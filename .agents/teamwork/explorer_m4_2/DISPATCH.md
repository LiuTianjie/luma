## 2026-10-15T18:27:37Z
[Message] timestamp=2026-10-15T18:27:37Z sender=1387c386-51c1-4a2c-b02a-a266794155bd priority=MESSAGE_PRIORITY_HIGH content=You are Explorer M4-2.
Your working directory is: /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_2
Your parent is the Project Orchestrator (Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd).
Project root: /Users/turning4th/infra-stacks

MANDATORY: Read the authoritative user request at:
/Users/turning4th/infra-stacks/.agents/teamwork/ORIGINAL_REQUEST.md
Read the project architecture and milestone plan at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/PROJECT.md
Read DISPATCH.md at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/DISPATCH.md (Pay special attention to the URGENT USER FEEDBACK section!)
Read TEST_INFRA.md at:
/Users/turning4th/infra-stacks/.agents/teamwork/TEST_INFRA.md

Scope & Objective:
For Milestone 4 (M4: Node Self-Healing & Zero-Downtime Migration), investigate Feature 8: 4-Step Zero-Downtime Migration Engine Hardening & Standalone Script:
1. 4-step workflow in `luma/paas/migration/migrator.py`:
   - `plan -> shadow -> cutover -> abort -> finalize`.
   - Batch 2 queue-worker race prevention (`serverx`, `granary`, `codex-gitea`): during `shadow`, worker replicas must be 0 while web/API runs and is probed; during `cutover`, drain Nomad workers before scaling K3s workers.
   - Batch 3 stateful volume & single-instance bot isolation (`kato`, `bot`):
     - Explicit `node` pin matching legacy Nomad allocation host.
     - Preserving hostPath + `fsGroup`/UID/GID without copying data.
     - **USER REQUIREMENT**: NEVER simultaneously mount and write the same hostPath volume from both Nomad and K3s (corrupts SQLite WAL/flock) or run dual bot long-polling connections!
       - During `shadow`: stateful/exclusive-lock apps must keep `replicas: 0` (only pre-pull image, sync secrets, validate hostPath/node-pin).
       - During `cutover`: STOP the Nomad allocation BEFORE scaling K3s Deployment to `1`.
   - Same-node coexistence & port conflicts: Edge Traefik (`80`/`443` and `tcp-relay` ports) must NOT conflict during shadow; verify memory headroom.
   - Pre-cutover validation gates: `release.gateway_probe_ok is True`, secret parity verified, image architecture matches host.
2. Standalone one-time migration script (`scripts/migrate-nomad-to-k3s.py`):
   - Design a self-contained, robust script that operators run once to migrate workloads and de-commission Nomad (`systemctl disable --now nomad`).
3. Detail exact method signatures, diffs, and verification steps.

Constraints:
- You are read-only. DO NOT modify any code.
- Report all file paths, line numbers, concrete findings, and exact remediation code snippets.
- Write your comprehensive report to `/Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_2/handoff.md`.
- Send a completion message to parent.
