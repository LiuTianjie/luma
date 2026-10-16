## 2026-10-15T18:27:38Z
You are Explorer M4-3.
Your working directory is: /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_3
Your parent is the Project Orchestrator (Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd).
Project root: /Users/turning4th/infra-stacks

MANDATORY: Read the authoritative user request at:
/Users/turning4th/infra-stacks/.agents/teamwork/ORIGINAL_REQUEST.md
Read the project architecture and milestone plan at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/PROJECT.md
Read DISPATCH.md at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/DISPATCH.md
Read TEST_INFRA.md at:
/Users/turning4th/infra-stacks/.agents/teamwork/TEST_INFRA.md

Scope & Objective:
For Milestone 4 (M4: Node Self-Healing & Zero-Downtime Migration), design the automated unit test suite for `tests/test_paas_engine.py`:
1. Review Suite 4 (`MigrationReliabilityAndNodeSelfHealingTests`) in `TEST_INFRA.md` lines 98–110 and lines 720–900.
2. Design comprehensive unit tests covering:
   - openSUSE MicroOS self-healing evaluation (transactional rootfs, btrfs scrub/trim, SELinux `restorecon`).
   - macOS Darwin `<local>` `NO_PROXY` evaluation.
   - Daemonset YAML manifest verification (`deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`).
   - 4-step migration workflow (`plan`, `shadow`, `cutover`, `abort`, `finalize`).
   - Batch 2 queue-worker replica 0 shadow & drain cutover.
   - Batch 3 stateful volume node pin & single-instance bot isolation (`shadow` replicas 0, stop Nomad before scale 1).
   - Pre-cutover gates: `gateway_probe_ok`, secret parity, architecture matching.
   - Same-node Traefik port conflict protection and memory headroom checks.
3. Provide complete, executable Python test code ready for the M4 worker.

Constraints:
- You are read-only. DO NOT modify any code.
- Write your report to `/Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_3/handoff.md`.
- Send a completion message to parent.
