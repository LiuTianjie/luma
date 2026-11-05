---
name: luma-shell
description: Open interactive Luma node or application-container terminals from a local terminal without SSH. Use for remote shell access, bastion-like connections, direct versus Control-relayed transport, or terminal connection and latency troubleshooting. Routine log and trace searches belong to luma-logs.
---

# Luma interactive terminals

Use the installed CLI and its saved Control login. These commands first ship in
Luma 0.2.40. Check `luma node shell --help` and `luma app shell --help` outside a
source checkout if the installed version is uncertain; repository imports can
hide an older installed CLI.

```sh
luma node list --format json
luma node shell NODE
luma app list --format json
luma app shell FULL_SERVICE_NAME
luma node shell NODE --transport direct
luma node shell NODE --transport relay
```

Reuse an already known node name/ID or full service name. Container shells require
a running allocation and a node supporting container terminals; system stacks
are excluded. A node shell runs as the node Agent's OS user, which is commonly
root. Keep commands within the user's requested operation.

## Choose the route

- Default `--transport auto` authenticates through Control, then tries reachable
  node addresses in parallel. It falls back to Control relay when direct setup
  fails. Read the CLI's `direct` / `relay` message before describing the route.
- `direct` requires direct connectivity and fails instead of falling back.
  Node supervisors listen on TCP 17443 by default. Closed firewall/security-group
  ports prevent direct access; there is no NAT hole punching. Do not open ports
  or change networking merely as a retry of an unsuccessful shell connection.
- `relay` needs the node's existing outbound connection to Control, with no new
  node inbound port. It is available behind closed inbound firewalls as long as
  the Agent remains connected to Control.

Both routes use Luma WebSockets, not ordinary SSH or Tailscale SSH. Direct TLS
checks the certificate fingerprint provided through authenticated Control before
sending a short-lived, single-use session ticket. Management tokens go only to
Control. Use saved contexts or `--control-context NAME`; do not extract Agent
credentials, copy a management token to a node, or implement raw ticket calls.

## Interact and disconnect

Both stdin and stdout must be TTYs. If the execution tool supports a PTY, allocate
one and retain its session for input/output. Ctrl-C reaches the remote foreground
process; Ctrl-] disconnects locally; `exit` ends the shell and returns its exit
code. Resizing is forwarded automatically. A disconnected session is not resumed
or replayed automatically, so inspect state before repeating a mutation.

These commands have no JSON output, file-transfer mode or non-interactive `exec`
mode. Without a PTY, use supported structured commands such as `luma node status`,
`luma app logs` or `luma app log-query` for the relevant evidence, or provide the
interactive command for the operator. Do not pipe scripts into `shell`.

## Versions and slow terminals

For direct transport, upgrade Control, the target node Agent and the local CLI to
0.2.40 or newer. An updated CLI can use relay with older terminal-capable Control
and agents. A client upgrade does not upgrade servers. Follow the operator's
upgrade scope; normal cluster upgrades are available in Dashboard → Settings →
System maintenance, with Control before worker agents.

Dashboard terminals still use relay. CLI relay uses essentially the same network
path, so switching clients is not proof that latency is fixed. Compare actual
routes and network responsiveness, node/Control load and browser rendering before
assigning a cause; use `luma node status NODE` for readiness and resource evidence.

This skill is bundled with the CLI. First-time assistants use `luma skills install
--agent codex`, `--agent claude`, or `--path PATH`. Existing suites sync on normal
CLI startup after an upgrade; `luma skills sync` refreshes them explicitly and
backs up local edits. Check `luma skills status --format json`, then reload loaded
instructions or start a new agent conversation.
