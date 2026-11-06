---
name: luma-shell
description: Execute remote node commands for AI agents and scripts, or open interactive node and container terminals through Luma without SSH. Use for command execution, terminal access, relay latency and direct-connection troubleshooting. Routine log and trace searches belong to luma-logs.
---

# Luma commands and terminals

Use the installed CLI and its saved Control login. Check help outside a source
checkout: imports can hide an older installed CLI. Interactive shells first shipped
in 0.2.40. `node exec` first ships in 0.2.42 and needs CLI, Control and target Agent support for `exec-v1`;
upgrade the component named in a capability error. Do not emulate exec by injecting
commands into an interactive shell.

## Commands for agents and scripts

Use `exec` to replace `ssh host 'command'`. It requires no TTY, client Tailscale
membership or open node inbound port. Default transport is **relay**, avoiding a
pointless direct-port probe for each command. Commands run as the Agent's OS user,
commonly root. Keep commands within the requested operation.

```sh
luma node exec bd-prod -- uname -a
luma node exec --timeout 30 bd-prod -- sh -lc 'uptime; df -h'
luma node exec --stdin bd-prod -- sha256sum < artifact.tar
luma node exec --stdin --timeout 60 bd-prod -- sh -s < inspect.sh
luma node exec --diagnose bd-prod -- id
```

Put Luma flags **before the node name**. The remainder is literal executable/argv;
use `sh -lc '...'` explicitly for pipelines, expansion and compound commands. Do not
interpolate untrusted text into shell code. Stdin receives EOF immediately unless
`--stdin` forwards it. Stdout/stderr remain separate binary streams without PTY echo.
Normal output contains only command output; diagnostics go to stderr.

Local exit status mirrors the remote status. Missing executable: 127; permission
denied: 126; deadline: 124 (default 300 seconds, maximum 86400); Ctrl-C: 130; SIGTERM:
143; transport failure: 255. Transport failure can leave completion unknown: inspect
state before retrying a mutation. No reconnect or command replay occurs. Disconnect
and timeout cancel the supervised process group; over a broken network cancellation
is best-effort until the Agent observes disconnection or its deadline expires.
Silent exec commands follow their requested deadline, not the interactive shell's
idle timeout. Activity cannot extend that deadline. Use ordinary service/job
management for durable work.

SSH migration is not a text substitution: exec does not read `~/.ssh/config`.
Resolve aliases to registered Luma nodes and verify the Agent's OS user, HOME,
PATH and working directory. Use explicit, authorized user/shell wrappers when the
old SSH command depended on another account or login environment.

## Interactive terminals

```sh
luma node list --format json
luma node shell NODE
luma app shell FULL_SERVICE_NAME
```

Reuse known identifiers. Container shells need a running allocation and a compatible
Agent; system stacks are excluded. Both stdin and stdout must be TTYs. Allocate a
PTY and retain the session. Ctrl-C reaches the remote process; Ctrl-] disconnects;
`exit` ends the shell. Do not pipe scripts into `shell`; use `node exec --stdin`.

## Network routes and slow relay

An ordinary Internet client connects to Control over HTTPS/WSS; the node Agent
maintains an outbound connection. **Relay is the baseline when node inbound ports
are closed.** Never assume clients can reach cluster LAN/Tailscale addresses or
should join a VPN.

Interactive `shell` defaults to `auto`: authenticate, race node addresses, and fall
back to relay if setup fails. `exec` defaults to relay; `--transport auto` opts into
probing. `--transport direct` fails when no node address is reachable; it does not
punch through firewalls. Direct can mean LAN or Tailscale, not public TCP. Read the
displayed peer address before making a network claim. Do not open ports or change
firewalls/VPN/proxy configuration merely as a retry.

```sh
luma node shell NODE --transport relay --diagnose
luma node exec --diagnose NODE -- uptime
```

Diagnostics report Control socket peer and WebSocket ping RTT on stderr. This RTT
measures client-to-Control, not the full command path. Compare small-command round
trips and proxy/CDN routing before blaming rendering. A fake-IP peer such as
`198.18.*` indicates a virtual network path, not a remote origin or proven proxy
rule. Large network RTT cannot be removed by tuning PTY buffering.

When an operator supplies a reachable public Control origin IP, terminal commands'
`--resolve-ip IP` preserves the hostname, SNI and certificate verification. This
bypasses DNS/application proxy discovery, not system-wide TUN routing, and cannot
open a closed origin port. Never add `--insecure` as a latency fix. Node terminal
`resolveIp` likewise preserves verified TLS; do not use an IP endpoint URL.

To shorten the Agent-to-Control leg without changing client requirements, operators
can set `terminalRelay: {"resolveIp": "CONTROL_PUBLIC_ORIGIN_IP"}` in the node Agent
config. This affects only the terminal connection, uses verified TLS and falls back
to the normal Control route when unavailable. TLS verification remains enabled on
both the preferred and fallback routes when this preference is configured. It is preserved during Agent upgrades.
Existing commands are canceled on a broken connection, never replayed on fallback.
Verify the public origin and obtain authorization for node configuration changes;
skill installation alone does not enable this route.

Both routes use Luma WebSockets. Direct TLS verifies Control's certificate fingerprint
before sending a single-use session ticket. Management tokens stay on Control. Do
not extract Agent credentials, copy management tokens to nodes or implement raw
ticket calls.

CLI/skill installation does not update servers. Follow the user's upgrade scope,
with Control before Agents. Skills ship with the CLI and sync on upgrade; inspect
with `luma skills status --format json` or refresh with `luma skills sync`, which
backs up local edits. Source-checkout checks do not prove installed/live support.
