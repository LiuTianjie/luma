# Nomad security defaults and automatic upgrades

`luma bootstrap` and `luma node join` provision ACLs, mutual TLS, introduction identities and persistent network rules on first use. The installer checks the running configuration before reporting success. No separate sequence of security commands is required.

For existing clusters, update the manager with `luma update manager` (or Dashboard **Update Control**), then update all node agents with the normal fleet or local update command. Watch **Cluster security upgrade** in the system update page. Package versions and security migration have separate results; only **All checks passed** confirms migration completion.

The manager installs an independent root service, `luma-nomad-security.service`. It waits for authenticated upgraded agents, checks recorded deployments and real storage sources, persists each step, and survives Control replacement and Nomad restarts. Offline nodes remain explicitly pending. Unknown live jobs, conflicting identities or unsafe configurations stop migration with a reason. Fix the cause and rerun the ordinary manager update to inspect and resume.

## Upgrade behavior

The automatic path supports a Luma-managed single Linux Nomad server, Linux/macOS clients, the `global` Nomad region, `dc1`, and `/opt/nomad/data`. From 1.9.x it traverses **1.10.5 → 1.11.3 → 2.0.7 → 2.1.1**, skipping completed releases. Each round upgrades the server first, then every client, verifying health and stable node identities before continuing. Official SHA-256 values for all four platforms are pinned in source.

Before each server upgrade, Luma saves and inspects a private Raft snapshot and retains the old configuration/binary. It stages role credentials and certificates, rolls RPC TLS with a temporary compatibility mode, then activates server ACLs and the final strict configuration everywhere. Compatibility mode must be off before final success.

Running jobs must match Control's saved deployment artifacts. Names and `luma.managed` labels confer no authority. Mounts are checked on their actual nodes, including resolved paths, without changing data sources or ownership. Ordinary workers allow only Docker, disable privileged containers and arbitrary host volumes. The normal deployment flow verifies the single storage owner and enables a controlled storage profile when an application first needs persistent mounts.

Linux bind sources are resolved on the host. macOS Docker runs in a separate VM, so the automatic path accepts only local named volumes without driver options there. Unproven macOS bind sources stop with an explicit diagnostic; a Mac filesystem check is not treated as VM filesystem evidence.

Trusted application rendering records an authority profile, allowing normal image/command/environment updates without a manual review. It cannot authorize devices, privilege escalation, dangerous namespaces, host roots, engine sockets or host credential paths. System bootstrap grants pin complete job specs. Existing application workflows remain in place. Traefik and optional Observe readers adopt scoped credential mounts; their exact allocation revisions and Observe's real Nomad-read metrics must pass verification.

New job mutations wait while migration applies node changes. Additional HCL/JSON fragments, external ACL/CA ownership and non-default namespaces stop with an explicit diagnostic instead of being overwritten.

## Boundaries and verification

- Administrator token, CA signing key, upgrade journal and backups: `/etc/luma/nomad`, private to the host and outside Control's `/opt/luma` mount.
- Consumer credentials: `/opt/luma/security/nomad/<role>.d`; each reader mounts only its own directory read-only. Job JSON contains paths, never token/private-key values.
- Node identities: local `/etc/luma/nomad`, delivered over authenticated Luma channels. Renewal tasks contain public CSRs/certificates, not private keys.
- Public progress: `/opt/luma/security/nomad/upgrade-status.json`, surfaced through the management API and system update page.
- Audit: `/opt/luma/control/security-audit.jsonl`, with socket peer, credential fingerprint, request identity and job digest, without secret bodies.

Service tokens do not unexpectedly expire after 24 hours. Explicit rotation stages replacements before old accessors are revoked. Certificates last one year and renew 30 days before expiry; a returning offline node can renew through its authenticated Agent channel even if its old Nomad certificate expired.

New agents retire legacy `insecure`/`resolveIp` settings after verifying Control HTTPS through normal DNS. Verification failure stops task consumption and reports the DNS/certificate problem. Old agents do not receive the new Nomad reader credential. Neither Control nor Nomad requests follow redirects with credentials.

HTTP binds to loopback and the local Tailscale IP; RPC/Serf bind to Tailscale. Linux uses an independent nftables table before Docker/CNI NAT and accept rules, including IPv6 and forwarding. macOS uses a PF anchor and rediscovers the Tailscale interface. Network guards must apply before Nomad starts. An entire tailnet CIDR is never an API/RPC allowlist.

Final checks cover versions, original node identity, anonymous denial for job lists, details and historical versions (including their Env), role permissions, HTTP/RPC mutual TLS, disabled compatibility mode, strict enrollment, live driver and volume settings, private listeners, configuration digests and applied network guards. Missing credentials never fall back to anonymous requests.

Control remains a trusted infrastructure administrator with its existing Docker management capability. Nomad Community submit permission is not a general sandbox or admission controller. Do not share Control management, Nomad administrator or host root authority with applications.

Job-only rollback cannot restore a revision that lacks current credential mounts or storage-node restrictions. Redeploy the desired image through the normal Luma workflow so it receives the current protections.

## Recovery and rollback

Inspect the failed step in the dashboard and `journalctl -u luma-nomad-security`. The private `/etc/luma/nomad/upgrade.json` records progress. Do not publish this directory, full job artifacts or Raft snapshots in tickets.

Temporary Control outages and offline nodes wait at the saved step. Interrupted/timed-out agent tasks retry the same typed request a bounded number of times. A private host operation record recovers a replaced binary/configuration even if the process stopped before restarting Nomad. Consumer updates persist both the previous and intended jobs before submission, verify the actual revision, and do not restart an already updated consumer on every retry.

Downloads try the configured proxy and then direct access. If both fail, the installed binary is retained and the step reports failure. For download, configuration, identity or authority failures, fix the cause and rerun `luma update manager` to continue the saved migration. Do not delete bootstrap credentials, create an ACL reset file, regenerate the CA, disable ACLs or disable TLS to recover connectivity.

Nomad does not support in-place binary downgrades. Restore a verified snapshot, matching version/configuration/credentials and application data onto separate trusted hosts in an isolated network. Verify jobs, nodes and routes before switching traffic. Raft snapshots do not contain application volume data.

A host on which an attacker obtained root still requires a trusted rebuild and rotation of exposed credentials. Installing these protections cannot establish that the old operating system is clean.

This change does not redesign business-secret storage in Control's database or runtime Env, nor invalidate exposed secrets. Encryption at rest and workload secret delivery are separate work; database encryption alone cannot protect against host root that retains the decryption authority.

References: [version upgrade notes](https://developer.hashicorp.com/nomad/docs/upgrade/upgrade-specific), [upgrade and downgrade limitations](https://developer.hashicorp.com/nomad/docs/upgrade), [official release archives](https://releases.hashicorp.com/nomad/).
