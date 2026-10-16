## 2026-10-15T18:54:22Z
You are Challenger M4-2.
Your working directory is: /Users/turning4th/infra-stacks/.agents/teamwork/challenger_m4_2
Your parent is the Project Orchestrator (Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd).
Project root: /Users/turning4th/infra-stacks

MANDATORY: Read the authoritative user request at:
/Users/turning4th/infra-stacks/.agents/teamwork/ORIGINAL_REQUEST.md
Read the project architecture and milestone plan at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/PROJECT.md
Read TEST_INFRA.md at:
/Users/turning4th/infra-stacks/.agents/teamwork/TEST_INFRA.md
Read the Worker M4 handoff at:
/Users/turning4th/infra-stacks/.agents/teamwork/worker_m4/handoff.md

Objective:
Adversarially challenge and stress-test Feature 8 (4-Step Migration Engine, SQLite/flock Single-Instance Isolation, Standalone Script & CLI Cleanup):
- Stress test SQLite / flock single-instance volume isolation: verify that in `shadow()`, stateful workloads (`kato`, `bot`) have strictly `replicas == 0`.
- Stress test two-phase cutover: verify that in `cutover()`, `legacyNomadAllocationStopped` is True and Nomad allocation is stopped before scaling K3s Deployments to 1.
- Stress test pre-cutover validation gates:
  - `gateway_probe_ok=False` -> must raise `LumaError`
  - volume missing `node` pin -> must raise `LumaError`
  - container arch != target node arch -> must raise `LumaError`
  - image digest drift -> must raise `LumaError`
  - secret parity mismatch -> must raise `LumaError`
- Stress test memory headroom: available_memory_mb = 511 -> must raise `LumaError`; 512 -> passes.
- Stress test CLI parser: verify `luma --help` does not contain `migrate`, but `luma migrate --help` outputs subcommands.
- Stress test standalone script `scripts/migrate-nomad-to-k3s.py` dry-run and batch options.
- Run Python verification scripts against the code using `./.venv/bin/python`.

Deliver your findings and explicit verdict (`APPROVE` or `REQUEST_CHANGES`) in `/Users/turning4th/infra-stacks/.agents/teamwork/challenger_m4_2/handoff.md`.
Send a completion message to parent.
