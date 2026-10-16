## 2026-10-15T19:54:30Z
You are Explorer M5-3.
Your working directory is: /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_3
Your parent is the Project Orchestrator (Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd).
Project root: /Users/turning4th/infra-stacks

MANDATORY: Read the authoritative user request at:
/Users/turning4th/infra-stacks/.agents/teamwork/ORIGINAL_REQUEST.md
Read the project architecture and milestone plan at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/PROJECT.md
Read TEST_INFRA.md at:
/Users/turning4th/infra-stacks/.agents/teamwork/TEST_INFRA.md

Objective:
Investigate Bilingual Documentation Parity & CLI Reference Generation for Milestone 5:
1. Examine `docs/migration-guide.md` and `docs/migration-guide.zh-CN.md`:
   - Inspect required updates based on user feedback:
     a. Reference `scripts/migrate-nomad-to-k3s.py` as the canonical standalone runner (supporting `--dry-run`, `--batch [1,2,3]`, `--project`).
     b. Document real-world hazards: same-node Traefik (`80`/`443` & `tcp-relay`) port conflict avoidance & memory headroom checks (>= 512MB).
     c. Document stateful SQLite / flock volume isolation (`kato`, `bot`) & single-instance bot protection: shadow keeps `replicas: 0`, and cutover stops Nomad allocation before scaling K3s to 1.
     d. Document post-migration Nomad decommission: `systemctl disable --now nomad`.
     e. Document 0-to-1 fresh install experience vs. existing user migration compatibility.
   - Check strict bilingual parity:
     - Heading structure and anchors must match 100%.
     - Code block count and commands must match 100%.
     - Exact terminology and technical clarity in both English and Simplified Chinese.
2. Examine CLI reference generation:
   - Check `scripts/generate-cli-reference.py` and `docs/luma-cli-reference*.md`.
   - Run `./.venv/bin/python scripts/generate-cli-reference.py --check` (or test without `--check` to see diff).
   - Check if `migrate` is omitted from generated CLI references due to `argparse.SUPPRESS`.
3. Check static site build and validation:
   - Run `./.venv/bin/python scripts/build-site.py` and `./.venv/bin/python scripts/check-site.py` to check for any broken links, missing anchors, or markdown errors across the 48 pages.
4. Detail all exact edits required to achieve 100% clean check-site and check-luma passes.

Constraints:
- You are read-only. DO NOT modify any code.
- Write your comprehensive report to `/Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_3/handoff.md`.
- Send a completion message to parent via send_message with the handoff path.
