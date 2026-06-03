# Compose Deployments And Storage

Luma can deploy a standard `docker-compose.yml` with a sidecar file named `luma.compose.yml`.

Use this path for multi-service applications that already have a Compose service graph. Keep the Compose file standard so it remains useful for local development. Put Luma-specific deployment semantics in the sidecar: region, exposure, routing, local node pins, and references to manager-managed storage classes.

Storage services are manager-owned infrastructure. A sidecar can reference `storageClass: home-nfs`, but the provider, storage node/path, external endpoint, and eligible regions/nodes live in Luma Control state. For managed storage, Luma resolves the actual NFS endpoint at render/deploy time from the service region and storage node reachability. Luma Control rejects non-empty `storageClasses` blocks in submitted sidecars so deleting or editing one deployment file cannot accidentally delete or redefine shared storage infrastructure.

## Prerequisites

- Bootstrap a Luma manager with `luma bootstrap manager`.
- Join workload or storage nodes with `luma node join --name <node-name> --region <cn|global|home>`.
- Login from the client or set stateless control auth:

```bash
luma login https://luma.example.com --token <deploy-token>

# or in CI
export LUMA_CONTROL_URL="https://luma.example.com"
export LUMA_DEPLOY_TOKEN="$CI_LUMA_DEPLOY_TOKEN"
```

Use the Luma node name when declaring node pins. For the first manager-hosted storage service, you may also use the manager Swarm hostname shown by `luma status`, because the manager might not have gone through `luma node join`.

## Declare Storage Services

Register storage classes against Luma Control. This writes manager state; it does not edit any deployment file.

The simplified storage CLI requires the manager Control API from this release. If `luma storage set ... --node ... --path ...` returns `endpoint is required for nfs`, the client is talking to an older manager. Run the installer on the manager, then run:

```bash
luma update manager --domain <control-domain>
```

```bash
luma storage set home-nfs \
  --node home-nas \
  --path /srv/luma \
  --region cn \
  --region home

luma storage list
```

To use the current manager as the first managed NFS node:

```bash
luma update manager --domain <control-domain>
luma status
luma storage set cn-nfs \
  --node <manager-swarm-hostname> \
  --path /srv/luma \
  --region cn
```

If the manager has not yet been written into Luma's node registry, Control adopts the matching Swarm node during `storage set`. The manager node still needs a region label; the normal bootstrap/update flow writes it from the manager profile.

The command means:

- `home-nfs` is the stable storage class name deployments reference.
- `--provider nfs` is optional; NFS is the only v1 user-facing provider.
- `--node home-nas` pins that storage component to a registered Luma node, or to the manager Swarm hostname for first manager-hosted storage.
- `--path /srv/luma` is both the host persistent directory and the managed NFS export root.
- `--region` values restrict which service regions may use the class.

Managed endpoint resolution is automatic:

- Same service region as the storage node: Docker mounts `<storage-node-name>:<path>`.
- Different service region from the storage node: Docker mounts `<storage-node-tailscale-ip>:<path>`.
- Cross-region managed storage requires the storage node to have reported `tailscaleIP` during `luma node join`; otherwise validate/render/deploy blocks and asks you to rerun join or refresh node labels.

`storageClass.regions` is the allowed business usage range. It does not prove network reachability; managed cross-region reachability comes from the storage node's Tailscale address.

For an existing external NFS server:

```bash
luma storage set company-nfs \
  --external \
  --endpoint nfs.example.com:/srv/luma \
  --region cn
```

External NFS uses the endpoint exactly as provided. Luma does not infer network paths for external storage, so at least one `--region` is required and external reachability remains the operator's responsibility.

The storage class remains in manager state until explicitly removed:

```bash
luma storage remove home-nfs
```

Removing a storage class only removes the manager declaration. It does not delete data from the storage backend.

## Sidecar Shape

Create the sidecar:

```bash
luma compose init --compose docker-compose.yml --output luma.compose.yml
```

Then edit it. Do not put `storageClasses` in this file for control-plane deployments.

```yaml
name: app-stack
compose: docker-compose.yml
region: cn

volumes:
  pg-data:
    storageClass: home-nfs
    path: postgres/pg-data
    accessMode: ReadWriteOnce

  cache-data:
    local:
      node: home-mac-mini
      path: /opt/luma/state/cache-data

services:
  app:
    exposure: cn-edge
    domain: app.example.com
    port: 3000

  postgres:
    region: home
```

Sidecar fields:

| Field | Required | Notes |
| --- | --- | --- |
| `name` | yes | Stack name. Reusing the same name updates the same Compose stack. Changing it creates a different stack. |
| `compose` | yes | Relative path to the standard Compose file. Control-plane deploys reject absolute paths and `..`. |
| `region` | yes | Default region for services without an override. Valid values are `cn`, `global`, and `home`. |
| `volumes.<name>.storageClass` | no | References a storage class registered in Luma Control. |
| `volumes.<name>.path` | no | Subdirectory under the storage class export. Defaults to the volume name. |
| `volumes.<name>.accessMode` | no | `ReadWriteOnce` or `ReadWriteMany`. Currently informational for rendering and dashboard diagnosis. |
| `volumes.<name>.initialize` | no | Use `empty` only when switching to a deliberately empty storage path. |
| `volumes.<name>.adopted` | no | Set `true` after manually migrating or adopting existing data. |
| `volumes.<name>.local.node` | no | Luma node name for explicit local bind storage. |
| `volumes.<name>.local.path` | no | Host path used when `local.node` is set. |
| `services.<name>.region` | no | Per-service region override. Must match exposure and storage restrictions. |
| `services.<name>.node` | no | Explicit Luma node pin for that service. |
| `services.<name>.exposure` | no | `none`, `cn-edge`, `external-edge`, `tailscale-relay`, or `cloudflare-tunnel`. |
| `services.<name>.domain` | public only | Public hostname. |
| `services.<name>.port` | public only | Container internal port. |
| `services.<name>.publishPort` | relay only | Host-mode published port for `tailscale-relay`. |
| `services.<name>.replicas` | no | Swarm replicas, must be at least `1`. |
| `services.<name>.proxy` | no | Adds Luma egress proxy env/network for runtime outbound HTTP/HTTPS traffic. |

## Validate And Preview

```bash
luma compose validate luma.compose.yml
luma compose render luma.compose.yml

luma storage check luma.compose.yml
luma storage apply luma.compose.yml --dry-run
```

`compose validate`, `compose render`, `storage check`, and `storage apply --dry-run` read manager storage classes and node reachability through the current login context or `--control-url/--token`. Production deploys must use manager-managed storage declarations. Local sidecar `storageClasses` are only for narrow offline render experiments; managed storage still needs node reachability data, so use a control context for realistic checks.

`storage check` reports each `service/volume -> storageClass -> resolved endpoint -> path mode` plan. It also blocks on storage class region/node restrictions and managed cross-region storage nodes without `tailscaleIP`. Docker performs NFS mounts on the workload node using the rendered local volume driver options when the task starts.

## Apply Managed Storage

For managed NFS, deploy the storage component before deploying dependent applications:

```bash
luma storage apply luma.compose.yml --dry-run
luma storage apply luma.compose.yml
```

The storage component stack is manager-scoped by storage class name, for example `luma-storage-home-nfs`. Multiple Compose deployments that reference the same class use the same storage infrastructure.

Managed NFS is a convenience component, not high-availability storage. For production HA state, use an external NFS service, a distributed provider, or a managed database/object store.

## Deploy And Update

Deploy:

```bash
luma compose deploy luma.compose.yml --dry-run
luma compose deploy luma.compose.yml --format ndjson
```

`compose deploy` submits both the sidecar and Compose file to Luma Control. The manager renders one Swarm stack, writes generated files under the configured stack root, syncs DNS for public services, deploys through Portainer, and probes public routes.

Update by editing `docker-compose.yml` and/or `luma.compose.yml`, then run the same deploy command again. The same sidecar `name` maps to the same Portainer stack. Re-running deploy updates that stack instead of creating a duplicate.

Storage backend changes are guarded. If an existing deployed volume changes from unmanaged/local to `storageClass`, or from one storage backend to another, deploy blocks unless the sidecar explicitly says how to treat the data:

```yaml
volumes:
  pg-data:
    storageClass: home-nfs
    path: postgres/pg-data
    adopted: true
```

Use `adopted: true` only after manually copying or verifying the existing data. For a fresh empty path, use:

```yaml
volumes:
  pg-data:
    storageClass: home-nfs
    path: postgres/pg-data
    initialize: empty
```

`initialize: empty` is an explicit data-loss acknowledgement for that path. It does not delete old local Docker volumes.

## Migrate Existing Data

Luma does not guess where old data lives and does not automatically copy state. Plan the migration explicitly:

```bash
luma storage migrate luma.compose.yml \
  --volume pg-data \
  --from-node home-mac-mini \
  --from-volume pg-data
```

Then run a maintenance copy job or host-level copy procedure appropriate for your environment. After verifying the copied data, set `adopted: true` for that sidecar volume and redeploy.

## Remove

Remove the Compose stack:

```bash
luma compose remove luma.compose.yml --dry-run
luma compose remove luma.compose.yml
```

This removes the application Portainer stack, generated route files, and DNS records for public services. It does not delete storage data and does not remove manager storage class declarations. Remove storage class declarations separately with `luma storage remove <name>` only when no deployments depend on them.

## Storage Rules

- `storageClass` is the Luma-managed path. The sidecar references the class by name; Luma Control provides the storage declaration from manager state and Luma resolves the service-specific endpoint during validation/render/deploy. For `provider: nfs`, Luma renders Docker local volume driver options with NFS mount settings, so application tasks mount the NFS export through Docker.
- If the same top-level Compose volume is used by services in different regions and managed storage would resolve to different endpoints, validation fails. Split the data into region-specific volume names instead.
- `local.node` is allowed for explicitly node-pinned local state. Luma rewrites the mount to a bind path and pins every service using that volume to the specified Luma node.
- Bare compose volumes are allowed, but Luma marks them unmanaged. If Swarm reschedules the service, Docker may use a different node-local volume. Luma does not guarantee data consistency for unmanaged volumes.
- Switching an existing deployed volume from unmanaged/local to `storageClass` is blocked by default. Run an explicit migration first and set `adopted: true` on that volume after verifying copied data, or set `initialize: empty` when starting from a fresh storage path.
- Removing a compose stack does not delete storage data. Delete or migrate state separately.

## Local Node Volumes

`local.node` is a user-owned local bind mount:

```yaml
volumes:
  cache-data:
    local:
      node: home-mac-mini
      path: /opt/luma/state/cache-data
```

Luma pins every service using that volume to `home-mac-mini` and renders the service mount as a bind mount. If multiple local volumes force conflicting nodes for one service, validation fails. If a user uses local Docker volumes without declaring `local.node`, Luma warns but does not block deployment; scheduling consequences belong to the user.

## Dashboard

The dashboard Storage page shows manager storage classes, volumes detected from service labels, provider readiness, mounted services, and unmanaged-volume warnings. Service details also show whether each volume is `storageClass`, `local pinned`, or `unmanaged`.

## CI Usage

CI should use stateless control auth:

```bash
python -m pip install "luma-infra==0.1.26"

export LUMA_CONTROL_URL="https://luma.example.com"
export LUMA_DEPLOY_TOKEN="$CI_LUMA_DEPLOY_TOKEN"

luma compose validate luma.compose.yml --format json
luma storage check luma.compose.yml --format json
luma compose deploy luma.compose.yml --dry-run --format json
```

Main or release deployment:

```bash
luma storage apply luma.compose.yml --timeout 300
luma compose deploy luma.compose.yml --format ndjson --timeout 1800
```
