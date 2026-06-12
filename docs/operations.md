# Operations

Portainer is the required operations UI and deployment runner. Luma Control is the self-hosted API on the manager node that handles login tokens, node registration, DNS sync, stack rendering, and Portainer deployment calls. `luma deploy` talks to Luma Control; it does not SSH into a node to deploy.

The default path is:

```text
service.yaml -> luma deploy -> Luma Control -> render stack -> sync DNS -> Portainer API -> Docker Swarm
```

Luma deploys through the Portainer API.

## Add A Service

```bash
luma service new
luma deploy <service>.yaml --dry-run
luma login https://luma.example.com --token <management-token>
luma deploy <service>.yaml
luma doctor
```

For a hand-written manifest, keep the same fields:

```yaml
name: api
image: ghcr.io/me/api:2026-05-29-1
region: cn
exposure: cn-edge
domain: api.example.com
port: 3000
replicas: 2
```

For multi-service applications, keep `docker-compose.yml` standard and add a Luma sidecar:

```bash
luma compose init --compose docker-compose.yml --output luma.compose.yml
luma compose validate luma.compose.yml
luma compose deploy luma.compose.yml --dry-run
luma compose deploy luma.compose.yml
```

Storage services are registered in Luma Control with `luma storage set`; compose deployments only reference them by name. Luma Control rejects deployment-side storage class definitions, and backend switches require either verified `adopted: true` or `initialize: empty`. Storage ownership, local node pins, and NFS storage classes are described in `docs/compose-storage.md`.

## Update Image Tag

Change the manifest:

```yaml
image: ghcr.io/me/api:2026-05-29-2
```

Then deploy:

```bash
luma deploy api.yaml
```

## Scale Replicas

Change the manifest:

```yaml
replicas: 3
```

Then deploy:

```bash
luma deploy api.yaml
```

Temporary scale from a manager node:

```bash
sudo docker service scale api_api=3
```

Temporary commands do not update Git. Commit the manifest change afterward if it should persist.

## Pin To One Node

Use this only when the service depends on local disk, hardware, or a specific home/worker machine:

```yaml
region: home
node: home-mac-mini
```

`node` must be the Luma node name passed to `luma node join --name`. Luma resolves it to the Swarm NodeID before deploy and still keeps the `region` constraint, so the selected node must also be in that region.

## View Status

From Portainer, check stacks, services, tasks, logs, and node placement.

From a client:

```bash
luma context list
luma context use <cluster-id>
```

From a manager node:

```bash
sudo docker service ls
sudo docker service ps <stack>_<service>
sudo docker service logs --tail 200 -f <stack>_<service>
```

## Roll Back

Preferred path:

```bash
git revert <deploy-commit>
luma deploy <service>.yaml
```

Emergency Docker rollback:

```bash
sudo docker service rollback <stack>_<service>
```

## Remove A Deployment

Use the deployed service or Compose application name:

```bash
luma service remove <service>
```

The control plane uses the manifest recorded during the last successful deploy, deletes the Luma-managed Cloudflare DNS record for public services, removes the Portainer stack, and deletes generated manager files. The same command removes single-service and Compose deployments. Because the control plane stores the manifest, this also works for deployments created through the web UI when the client no longer has a local YAML file. For `tailscale-relay`, it also deletes `/opt/luma/routes/<service>.yml`. For `cloudflare-tunnel`, Cloudflare Tunnel public hostname cleanup is skipped because that hostname is still managed in Cloudflare Zero Trust.

Storage data is preserved by default. To intentionally delete removable storage referenced by the recorded deployment, preview and then run:

```bash
luma service remove <service> --dry-run --delete-storage
luma service remove <service> --delete-storage
```

For single-service deployments, this deletes managed storage paths referenced by `storage.<volume>.path` and removes named Docker volume objects declared in the manifest; bind mounts are skipped. For Compose deployments, this deletes managed volume subdirectories referenced by the sidecar, not the storage class itself. It cannot be combined with `--skip-portainer`.

Preview the cleanup without changing the manager:

```bash
luma service remove <service> --dry-run
```

Keep DNS or the Portainer stack when you are doing a partial cleanup:

```bash
luma service remove <service> --skip-dns
luma service remove <service> --skip-portainer
```

If the control plane is unavailable, remove the stack in Portainer, then remove generated files on the manager:

```bash
sudo rm -rf /opt/luma/stacks/<region>/<service>
sudo rm -f /opt/luma/routes/<service>.yml
```

If Portainer is unavailable:

```bash
sudo docker stack rm <service>
```

## Remove A Node

From any logged-in client:

```bash
luma node remove <node-name>
```

The request is handled by Luma Control on the manager. It deletes the Luma node registration and removes the matching Docker Swarm worker node using the saved NodeID or Luma node labels. Use this for stale nodes that already left locally, failed joins, or decommissioned worker/home machines. Manager nodes are protected and must not be removed through this command.

If a worker/home machine intentionally leaves Swarm and later rejoins with the same Luma node name, it receives a new Swarm NodeID. During join/update, Luma refreshes the saved `luma.node.id` label and updates Luma-managed services that were pinned to the old NodeID. Keep manifests pinned by Luma node name; do not replace them with Docker hostnames.

## Drain A Node

```bash
sudo docker node update --availability drain <node-name>
```

Restore it:

```bash
sudo docker node update --availability active <node-name>
```

## Refresh Egress

```bash
export EGRESS_SUBSCRIPTION_URL='...'
luma egress refresh
```

Verify image pulls on the target node:

```bash
sudo docker pull hello-world:latest
```

## Repair Control Plane

```bash
luma bootstrap manager --domain luma.example.com
luma portainer setup
luma doctor
```

## Required Network Ports

For Linux nodes, Luma configures UFW during bootstrap/join. Mirror the same access in cloud security groups or Tailscale ACLs:

| Port | Required between | Purpose |
| --- | --- | --- |
| `80/tcp` | public clients -> edge manager | HTTP redirect and Let's Encrypt challenge. |
| `443/tcp` | public clients -> edge manager | HTTPS ingress for Luma Control and public services. |
| `9443/tcp` | trusted operators -> manager | Direct Portainer UI/API. Prefer a restricted source range. |
| `tcp-relay` published ports | public clients -> edge manager | Public TCP relay ports, for example `3306/tcp` for MySQL. Luma restores Traefik listeners from Control state; cloud firewalls/security groups must allow the same ports. |
| `2377/tcp` | worker nodes -> manager | Swarm control plane. |
| `7946/tcp`, `7946/udp` | all Swarm nodes | Swarm discovery and overlay-network gossip. |
| `4789/udp` | all Swarm nodes | Overlay/VXLAN service traffic. |

Luma's Portainer deployment path uses a manager-constrained `portainer_agent` so `cn` deployments do not depend on worker-side Portainer agents. The Swarm ports are still required for workloads that run across multiple nodes.

## Portainer Access

Portainer is deployed on the manager's `9443` port for the first bootstrap experience:

```text
https://<manager-ip>:9443
```

The `luma bootstrap manager --domain ...` domain is for Luma Control, not Portainer. Portainer's stack has
Traefik disabled by default, so bootstrap does not create a Portainer domain.

The Portainer username defaults to `admin`. The generated password is stored on the manager:

```bash
sudo jq -r '.portainerAdminUsername, .portainerAdminPassword' /opt/luma/control/control.json
```

For production, prefer accessing Portainer through a trusted network, a restricted source IP, or a private
control-plane path. Keep the Portainer API credentials secret because they can update deployments.

## Tailscale Relay

`tailscale-relay` is explicit per service. It is suitable for home tools, previews, or low-frequency internal panels that need a public domain.

It is not the default path for normal public traffic.
