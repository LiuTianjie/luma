# LAE source and deployment policy

Canonical capability, security, verdict, manifest, placement, environment and
framework rules are bundled in `knowledge-pack.json`. The release test requires
that self-contained Skill copy to be byte-for-byte identical to the Controller's
versioned Knowledge Pack.

## Supported sources

| Source | Accepted input | Build path |
| --- | --- | --- |
| Local file | One `.html` file or an already-built static archive | Platform-owned static image |
| GitHub / HTTPS Git | Repository plus immutable resolved commit | Luma Builder analysis and build |
| Template | Immutable template version | Normal analysis/build/deploy path |

Local upload does not accept dynamic source, Dockerfile, or Compose. Put those sources in Git or select a template.

## Supported application shapes

- Static output.
- One or more HTTP services.
- Worker, internal, cron, or datastore services without public TCP exposure.
- Compose with multiple public HTTP services; one route is primary and every additional route receives its own stable random `*.itool.tech` hostname.
- Managed named volumes and application-contained databases, including Lite subject to quota. This is persistent storage, not a managed-database SLA.
- Dockerfile and Compose for Lite, Pro, and Ultra, subject to the same security policy and per-plan quotas.

## Compose image ownership

- Keep internal Registry coordinates platform-owned. Never add the retired
  `registry.itool.tech`, a Builder IP, `localhost:5000`, `registryHost`, or
  `pushHost` to user Compose, Luma sidecars, or manifest candidates.
- When several services run the same repository-built image, declare one
  unique `build:` owner and give every consumer the exact same logical image
  tag, preferably through a YAML anchor such as `app:local`. LAE binds those
  consumers to the owner's `buildKey`; they are not external images.
- Do not copy the same `build:` block onto every consumer and do not invent an
  internal image URL. Luma Builder chooses the internal repository and returns
  the immutable runtime digest.
- Treat an image-only service that does not exactly match a unique build owner
  as an external prebuilt image. Preserve its explicit non-`latest` public
  reference; direct deploy semantics pull exactly the user-declared image.
- Accept legacy source that still shares an old image tag with a unique build
  owner, but recommend replacing that source tag with a neutral logical tag.

## Always block

- Public TCP/UDP or `tcp-relay`.
- Host port publishing, host network/PID/IPC namespaces, privileged mode, devices, Docker/control sockets, host bind mounts, or added Linux capabilities.
- Paths escaping the source snapshot, unsafe symlinks, inline credentials, `.env`/private key/cloud credential leakage, or an unapproved source host.
- Mutable deployment/build plans supplied by the user. LAE generates and stores the Luma manifest outside the repository.
- A build whose required build argument/secret cannot be supplied through a task-bound short-lived credential mechanism.

## Environment variables

Return names, scope, required/sensitive/public flags, and consuming services. Never return or log values. Treat token, secret, password, key, and credential-like names as sensitive unless an adapter provides stronger evidence.

## Domain and updates

- Do not offer custom domains in V1.
- Keep app domains stable across update, restart, suspend/resume, and rollback.
- A floating Git ref update creates a new analysis and immutable source snapshot. Never reuse an old signed plan for a new commit.
- A topology, route, environment, or volume change requires a new plan; destructive diffs require human confirmation.
