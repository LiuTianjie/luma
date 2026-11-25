# Nomad security defaults and certificate maintenance {#nomad-security-defaults-and-automatic-upgrades}

`luma bootstrap` and `luma node join` provision ACLs, mutual TLS, introduction identities and persistent network rules on first use. Existing Control data encryption and application secret isolation remain enabled.

The one-time cluster security migration is retired from normal updates. Manager updates, node updates and the system update page no longer start, wait for or display it. Historical migration status no longer blocks deployments. Routine updates do not replay staged Nomad upgrades, secret migration or history cleanup.

`luma-nomad-security.service` now only handles certificate enrollment and renewal, retaining its service name and private identity records. It follows current registered nodes and defers offline nodes until they return. Maintenance errors are logged and retried without reopening a completed migration.

## Worker host access {#worker-host-access}

`luma node join` registers ordinary workers with `luma_host_access=isolated`. An isolated worker runs Docker workloads with host volume mounts restricted. A `trusted` worker enables Docker volume mounts for admitted workloads under Nomad ACLs and mutual TLS.

### Grant host access manually {#grant-host-access-manually}

Run the installed Luma CLI as root on the target worker. Use its registered name, current region, Tailscale address, Nomad RPC servers and enrolled TLS file paths. Carry forward any custom peer rules with `--rpc-peer`, `--http-peer` and `--serf-peer`. On macOS, supply the verified Tailscale interface with `--mesh-interface`.

The following example prepares a bundle on a Linux worker. Substitute the values for your own node.

```sh
luma security prepare-node \
  --output /root/worker-1-trusted.json \
  --name worker-1 --region home --role client \
  --mesh-ip 100.64.0.20 --server 100.64.0.10:4647 \
  --host-access trusted --replace-config \
  --tls-ca /etc/luma/nomad/tls/ca.pem \
  --tls-cert /etc/luma/nomad/tls/cert.pem \
  --tls-key /etc/luma/nomad/tls/key.pem
```

Review the generated Nomad configuration and firewall policy against the worker's settings. Confirm that the bundle sets `luma_host_access=trusted` and enables Docker volumes. Set `REVIEWED_SHA256` to the `sha256` printed for the reviewed bundle.

```sh
luma security apply-node \
  --bundle /root/worker-1-trusted.json \
  --expected-sha256 "$REVIEWED_SHA256"
```

`apply-node` checks the bundle digest, the baseline configuration digest and the installed Nomad version. It backs up the current Nomad configuration and service definition, then restarts that worker's Nomad service. On the Manager, run `luma security plan --output /root/luma-security-plan.json` and check that the worker reports `hostAccess` as `trusted`. Use `luma node status worker-1 --format json` to confirm that Nomad reports `ready` and `eligible` and that the Luma Agent has a fresh heartbeat.

### Grant host access during deployment {#grant-host-access-during-deployment}

When Control enforces workload admission, an actual Luma deployment with persistent Docker mounts must resolve to exactly one ready storage owner with a compatible Luma Agent. Control asks that Agent to validate the mounts and promotes an isolated owner to `trusted` when needed. The promotion restarts the owner's Nomad client before the application job is submitted.

## Boundaries and verification

Manager updates merge Control configuration through the encrypted storage codec without rescanning all history or vacuuming the live database. The installed storage encryption profile continues to protect new writes.

- Administrator token, CA signing key, upgrade journal and backups: `/etc/luma/nomad`, private to the host and outside Control's `/opt/luma` mount.
- Consumer credentials: `/opt/luma/security/nomad/<role>.d`; each reader mounts only its own directory read-only. Job JSON contains paths, never token/private-key values.
- Node identities: local `/etc/luma/nomad`, delivered over authenticated Luma channels. Renewal tasks contain public CSRs/certificates, not private keys.
- Audit: `/opt/luma/control/security-audit.jsonl`, with socket peer, credential fingerprint, request identity and job digest, without secret bodies.

Service tokens do not unexpectedly expire after 24 hours. Explicit rotation stages replacements before old accessors are revoked. Certificates last one year and renew 30 days before expiry; a returning offline node can renew through its authenticated Agent channel even if its old Nomad certificate expired.

New agents retire legacy `insecure`/`resolveIp` settings after verifying Control HTTPS through normal DNS. Verification failure stops task consumption and reports the DNS/certificate problem. Old agents do not receive the new Nomad reader credential. Neither Control nor Nomad requests follow redirects with credentials.

HTTP binds to loopback and the local Tailscale IP; RPC/Serf bind to Tailscale. Linux uses an independent nftables table before Docker/CNI NAT and accept rules, including IPv6 and forwarding. macOS uses a PF anchor and rediscovers the Tailscale interface. Network guards must apply before Nomad starts. An entire tailnet CIDR is never an API/RPC allowlist.

On macOS, installation and periodic firewall reconciliation share an exclusive lock. A `DIOCADDRULE: Resource busy` response receives a bounded retry; other errors still stop the operation. The guard acknowledges a policy only after its rules load successfully.

Final checks cover versions, original node identity, anonymous denial for job lists, details and historical versions (including their Env), role permissions, HTTP/RPC mutual TLS, disabled compatibility mode, strict enrollment, live driver and volume settings, private listeners, configuration digests and applied network guards. Missing credentials never fall back to anonymous requests.

Control remains a trusted infrastructure administrator with its existing Docker management capability. Nomad Community submit permission is not a general sandbox or admission controller. Do not share Control management, Nomad administrator or host root authority with applications.

Job-only rollback cannot restore a revision that lacks current credential mounts or storage-node restrictions. Redeploy the desired image through the normal Luma workflow so it receives the current protections.

## Maintenance and recovery {#recovery-and-rollback}

Read certificate maintenance logs with `journalctl -u luma-nomad-security`. Private enrollment and renewal records remain under `/etc/luma/nomad`. Offline nodes do not block Control updates or maintenance of other nodes.

Repair the relevant credential or certificate configuration when verification fails. Do not delete ACL bootstrap records, regenerate the CA or disable ACL/TLS to restore access.

## Encrypted storage and application identities

Control SQLite schema 2 encrypts configuration, deployment records, queued requests and history payloads with AES-256-GCM. Alert and callback credentials use the same authenticated codec. Row identity is authenticated, and missing keys or tampered ciphertext stop reads/writes without falling back to plaintext. Migration commits ciphertext atomically, then checkpoints and vacuums SQLite to remove old pages and WAL contents. Ordinary configuration/history API responses hide stored values; an unchanged `<redacted>` field in the same application's configuration preserves its existing value during an update.

The recovery key is outside the `/opt/luma` Control mount and application volumes. Control obtains its operational key through verified TLS and its own Nomad role from `luma/control/storage-key`. The storage profile contains the key identifier, not the key. Copying only the encrypted database is insufficient for disaster recovery. Control remains trusted to manage and decrypt the credentials it administers.

Each task receives only values rendered for that task from `nomad/jobs/<job>/<group>/<task>` in its own namespace. Nomad's built-in workload identity authorizes the native secret hook; Luma writes no shared job/group secret paths. Jobs contain references in Env, registry authentication and application arguments. The native hook resolves values in memory, and applications receive neither the Control recovery key nor a general-purpose Nomad reader token. Nomad starts with a clean environment so host environment variables cannot leak into unrelated tasks. Unknown workload-bound ACL grants stop migration for review.

Applications necessarily have their own plaintext values in memory/runtime environment; privileged host or Docker access can read them. Ordinary Nomad job readers cannot read the Variables. Readable job versions and allocation records contain references, and allocation APIs omit signed identities. Secret values are JSON-quoted before native env parsing, preserving quotes, newlines, Unicode and literal interpolation characters.

Migration replaces applications sequentially and waits for each exact revision to become healthy. A single-replica application may have a restart window. The supervisor archives its previous state, encrypts managed legacy artifacts/backups, removes old version tags and inline-secret job history, and collects retired allocations. It temporarily limits history to one version, then restores six protected versions for normal rollback. It also replaces old stored job sources with an empty source. Existing historical Raft log entries, filesystem snapshots and backups outside Luma's managed locations are not retroactively erased; previously exposed credentials still need rotation.

Job rollback checks that every referenced secret generation is still available before altering a healthy job. Schema 1 Control binaries cannot reopen schema 2 databases; automatic rollback to a pre-migration Control image or inline-secret job is disabled. Missing/offline nodes, a failed application rollout or unfinished old-allocation cleanup keep the upgrade incomplete.

Node introduction tokens last ten minutes. Nomad's default allocation identity is kept inside Nomad; it is not presented as a short-lived business credential. External provider keys keep the lifetime chosen by their provider. This release does not rotate provider accounts or replace all long-lived credentials with dynamic credentials.

References: [version upgrade notes](https://developer.hashicorp.com/nomad/docs/upgrade/upgrade-specific), [upgrade and downgrade limitations](https://developer.hashicorp.com/nomad/docs/upgrade), [official release archives](https://releases.hashicorp.com/nomad/).
