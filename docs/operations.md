# Operations

Production changes should flow through Git and Portainer.

The normal path is:

```text
service.yaml -> luma deploy --commit --push -> Portainer webhook -> Docker Swarm
```

## Add A Service

```bash
luma service new
luma deploy <service>.yaml --dry-run
luma deploy <service>.yaml --commit --push
luma doctor
```

For a hand-written manifest, keep the same fields:

```yaml
name: api
image: ghcr.io/me/api:2026-05-29-1
region: cn
public: true
exposure: cn-edge
domain: api.itool.tech
port: 3000
replicas: 2
```

## Update Image Tag

Change the manifest:

```yaml
image: ghcr.io/me/api:2026-05-29-2
```

Then deploy:

```bash
luma deploy api.yaml --commit --push
```

## Scale Replicas

Change the manifest:

```yaml
replicas: 3
```

Then deploy:

```bash
luma deploy api.yaml --commit --push
```

Temporary scale from a manager node:

```bash
sudo docker service scale api_api=3
```

Temporary commands do not update Git. Commit the manifest change afterward if it should persist.

## View Status

From Portainer, check stacks, services, tasks, logs, and node placement.

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
luma deploy <service>.yaml --commit --push
```

Emergency Docker rollback:

```bash
sudo docker service rollback <stack>_<service>
```

## Remove A Service

Remove the stack in Portainer, then remove generated files from Git:

```bash
rm -rf stacks/<region>/<service>
rm -f routes/<service>.yml
git add -A
git commit -m "remove <service>"
git push
```

If Portainer is unavailable:

```bash
sudo docker stack rm <service>
```

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
luma egress refresh aly
```

Verify image pulls:

```bash
ssh aly 'sudo docker pull hello-world:latest'
```

## Repair Control Plane

```bash
luma node bootstrap aly --profile single-node
luma portainer setup aly
luma doctor
```

## Portainer Access

Portainer is deployed on `9443` for the first bootstrap experience. For production, prefer accessing it through a trusted network, a restricted source IP, or a private control-plane path. Keep the webhook URL secret because it can trigger deployments.

## Tailscale Relay

`tailscale-relay` is explicit per service. It is suitable for home tools, previews, or low-frequency internal panels that need a public domain.

It is not the default path for normal public traffic.
