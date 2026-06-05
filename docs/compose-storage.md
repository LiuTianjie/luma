# Compose Deployments And Storage

Luma can deploy a standard `docker-compose.yml` with a sidecar file named `luma.compose.yml`.

Use this path for multi-service applications that already have a Compose service graph. Keep the Compose file standard so it remains useful for local development. Put Luma-specific deployment semantics in the sidecar: region, exposure, routing, local node pins, and references to manager-managed storage classes.

Storage services are manager-owned infrastructure. A sidecar can reference `storageClass: home-nfs`, but the provider, storage node/path, external endpoint, and eligible regions/nodes live in Luma Control state. For managed storage, Luma resolves the actual NFS endpoint at render/deploy time from the service region and storage node reachability. Luma Control rejects non-empty `storageClasses` blocks in submitted sidecars so deleting or editing one deployment file cannot accidentally delete or redefine shared storage infrastructure.

## Prerequisites

- Bootstrap a Luma manager with `luma bootstrap manager`.
- Join workload or storage nodes with `luma node join --name <node-name> --region <cn|global|home>`.
- Login from the client or set stateless control auth:

```bash
luma login https://luma.example.com --token <management-token>

# or in CI
export LUMA_CONTROL_URL="https://luma.example.com"
export LUMA_DEPLOY_TOKEN="$CI_LUMA_MANAGEMENT_TOKEN"
```

Use the Luma node name when declaring node pins. For the first manager-hosted storage service, you may also use the manager Swarm hostname shown by `luma status`, because the manager might not have gone through `luma node join`.

## Declare Storage Services

Register storage classes against Luma Control. This writes manager state; it does not edit any deployment file.

> [!NOTE]
> The sidecar's `volumes.<name>.storageClass` field does not declare the NFS storage server itself. It simply specifies that the named volume declared in Compose should map to a subdirectory within that storage class. The actual physical node, endpoint, and topology of the storage class are resolved dynamically by Luma Control from the registered storage class state.

To use the current manager as the first managed NFS node (recommended for single node or initial setups), register a class named `cn-nfs`:

```bash
luma status
luma storage set cn-nfs \
  --node <manager-swarm-hostname> \
  --path /srv/luma \
  --region cn
```

- `cn-nfs` is the stable storage class name deployments reference.
- `--node <manager-swarm-hostname>` identifies the host that owns and exports the storage path.
- `--path /srv/luma` is the host export directory.
- `--region cn` restricts which service regions can use this storage class.

When `--mount-options` is omitted, Luma uses bounded soft NFS options:

```text
nfsvers=4,rw,soft,timeo=100,retrans=10,noresvport
```

This keeps slow mounts from waiting forever in kernel I/O. Applications will receive an I/O error after the retry window is exhausted, so database workloads may fail and recover instead of remaining stuck in `D` state. Override with `--mount-options` when you need a different NFS policy.

For managed NFS on the local control node, `storage set` also prepares the host: it installs the NFS server/client packages when needed, creates the export directory, writes the NFS export, starts the host NFS service, and removes any legacy `luma-storage-*` storage stack left by older Luma versions. It does not delete data. If the target node is not local to the current Luma Control process, the command fails instead of saving a pending storage class; use an external NFS registration or run the operation from the control node that can prepare that host.

For an existing external or dedicated NFS node:

```bash
luma storage set home-nfs \
  --node home-nas \
  --path /srv/luma \
  --region cn \
  --region home
```

### Register an External Independent NFS Server

If using an external, non-Luma-managed NFS server, use the `--external` flag:

```bash
luma storage set company-nfs \
  --external \
  --endpoint nfs.example.com:/srv/luma \
  --region cn
```

### Unified Storage Service

Storage classes are infrastructure services. Every Compose named volume can use the same storage class model, including database container volumes such as PostgreSQL or MySQL data directories. Luma does not classify volumes by application type and does not require a type label or probe before a database volume can reference a storage class.

Register the storage service once:

```bash
luma storage set db-storage \
  --node storage-node \
  --path /srv/luma-db \
  --region home \
  --region cn
```

Then any named volume can reference that storage service and choose its own subdirectory:

```yaml
volumes:
  nextcloud-data:
    storageClass: db-storage
    path: nextcloud/nextcloud-data

  nextcloud-db:
    storageClass: db-storage
    path: nextcloud/nextcloud-db
```

Luma still validates storage topology: the storage class must exist, its allowed regions/nodes must match the service placement, and cross-region managed storage must have a reachable `tailscaleIP`. Those checks are about whether the storage service can be mounted from the scheduled node; they do not depend on whether the consuming container is a database, file app, cache, or any other application type.

---

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
    storageClass: cn-nfs
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

## ⚠️ Critical Storage Mounting Caveats

When configuring and deploying Luma-managed storage, pay attention to the following three points:

1. **Named Volume Declaration Match**
   The named volume referenced in the sidecar `luma.compose.yml` MUST be declared in the standard `docker-compose.yml`, and at least one service must mount it. For example:
   ```yaml
   # docker-compose.yml
   services:
     postgres:
       image: postgres:15
       volumes:
         - pg-data:/var/lib/postgresql/data

   volumes:
     pg-data: {}  # Must declare the named volume here
   ```

2. **Cross-Region Networking and Tailscale Dependency**
   If the storage service's physical node and the application service running node are in different regions, Luma will treat it as a cross-region managed storage mount.
   **Prerequisite**: The storage node must have a valid `tailscaleIP` (reported via `luma node join`). Without Tailscale reachability, `validate`/`render`/`deploy` will fail to resolve network paths and block execution.

3. **Consistent Class Names and Regions**
   To keep your layout clean, match your StorageClass names with their physical location and region scope. For example, for manager-hosted storage in region `cn`, name it `cn-nfs` and register it accordingly:
   `luma storage set cn-nfs --node <manager-swarm-hostname> --path /srv/luma --region cn`

---

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

For managed NFS, prepare storage before deploying dependent applications:

```bash
luma storage apply luma.compose.yml --dry-run
luma storage apply luma.compose.yml
```

`storage apply` resolves the manager storage classes and creates the concrete volume subdirectories referenced by the sidecar, for example `/srv/luma/app-stack/pg-data`. Compose deployments also run the same preparation step before deploying the application stack.

Managed NFS is a storage service. Luma treats each referenced named volume the same way: it prepares the configured subdirectory and renders Docker local volume driver options so the workload node mounts the storage service at task start.

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
    storageClass: cn-nfs
    path: postgres/pg-data
    adopted: true
```

Use `adopted: true` only after manually copying or verifying the existing data. For a fresh empty path, use:

```yaml
volumes:
  pg-data:
    storageClass: cn-nfs
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

Remove the Compose application by its deployed name:

```bash
luma service remove app-stack --dry-run
luma service remove app-stack
```

This removes the application Portainer stack, generated route files, and DNS records for public services. It does not delete storage data and does not remove manager storage class declarations. Remove storage class declarations separately with `luma storage remove <name>` only when no deployments depend on them.

To deliberately remove the managed storage paths referenced by this Compose deployment, add `--delete-storage`:

```bash
luma service remove app-stack --dry-run --delete-storage
luma service remove app-stack --delete-storage
```

This uses the Compose sidecar content recorded by the control plane during the last successful deploy, not a YAML file on the client running the command. It deletes only the application volume subdirectories declared in the sidecar, such as `volumes.pg-data.path`. It does not delete the storage class itself or unmanaged/external storage. `--delete-storage` cannot be combined with `--skip-portainer`.

## Storage Rules

- `storageClass` is the Luma-managed path. The sidecar references the class by name; Luma Control provides the storage declaration from manager state and Luma resolves the service-specific endpoint during validation/render/deploy. For `provider: nfs`, Luma renders Docker local volume driver options with NFS mount settings, so application tasks mount the NFS export through Docker.
- If the same top-level Compose volume is used by services in different regions and managed storage would resolve to different endpoints, validation fails. Split the data into region-specific volume names instead.
- `local.node` is allowed for explicitly node-pinned local state. Luma rewrites the mount to a bind path and pins every service using that volume to the specified Luma node.
- Bare compose volumes are allowed, but Luma marks them unmanaged. If Swarm reschedules the service, Docker may use a different node-local volume. Luma does not guarantee data consistency for unmanaged volumes.
- Switching an existing deployed volume from unmanaged/local to `storageClass` is blocked by default. Run an explicit migration first and set `adopted: true` on that volume after verifying copied data, or set `initialize: empty` when starting from a fresh storage path.
- Removing a stack does not delete storage data by default. Use `luma service remove <name> --delete-storage` only when you intentionally want to delete removable data referenced by the recorded deployment. For Compose this deletes managed storage paths referenced by the sidecar. For single-service manifests this deletes managed storage paths referenced by `storage.<volume>.path` and removes named Docker volume objects, while skipping bind mounts.

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
export LUMA_DEPLOY_TOKEN="$CI_LUMA_MANAGEMENT_TOKEN"

luma compose validate luma.compose.yml --format json
luma storage check luma.compose.yml --format json
luma compose deploy luma.compose.yml --dry-run --format json
```

Main or release deployment:

```bash
luma storage apply luma.compose.yml --timeout 300
luma compose deploy luma.compose.yml --format ndjson --timeout 1800
```
