# Exposure Model

Luma separates the control plane from the data plane.

## Roles

- Cloudflare: DNS automation, optional proxy, optional Tunnel public hostname. Other domains can live on Alibaba Cloud DNS or DNSPod; see [DNS zones](dns-zones.md).
- Node Traefik: optional per-node HTTP/HTTPS ingress for `node-edge` services.
- Traefik: main public HTTP/HTTPS ingress for the `cn` edge, plus `tcp-relay` published ports.
- Tailscale: management network and explicit relay path for `home` or enabled custom-region services.
- Luma Control: deployment control plane, driving the Nomad HTTP API.
- Nomad / Docker: runtime execution layer.
- Alibaba Cloud ESA: optional CDN in front of Traefik for public HTTP services; see [CDN](cdn.md).

Tailscale is not the default business data plane. It only carries data traffic when a service explicitly chooses `exposure: tailscale-relay`.

## Exposure modes

### `cn-edge`

Primary domestic public services.

```text
User -> Cloudflare DNS -> CN Traefik -> CN service
```

Use for:

- main websites;
- public Web/API;
- login, payments, dashboards, normal product traffic.

Manifest:

```yaml
name: app
image: ghcr.io/your-org/app:latest
region: cn
exposure: cn-edge
domain: app.example.com
port: 3000
replicas: 2
```

Luma generates:

- `stacks/cn/app/app.nomad.json`;
- Traefik service tags for the Nomad provider;
- Cloudflare DNS record pointing to the configured CN edge target.

By default, public traffic enters through one Traefik instance constrained to `${meta.region}=cn` and `${meta.ingress}=true`. If you have multiple CN nodes, DNS still points to the configured edge target; Traefik receives the request there and forwards to matching service allocations discovered through the Nomad provider.

### `tailscale-relay`

Home or custom-region services exposed through the existing edge over Tailscale. For a custom region, run `luma region update render --exposure tailscale-relay` before selecting that exposure in the app manifest. See the [upgrade and rollout guide](region-public-ingress.md).

```text
User -> Cloudflare DNS -> CN Traefik -> Tailscale -> home service
```

Use for:

- low-traffic tools;
- preview environments;
- home admin panels;
- services that are acceptable to depend on the home network.

Do not use for:

- core APIs;
- login or payment;
- large downloads;
- high-frequency product traffic.

Manifest:

```yaml
name: home-panel
image: ghcr.io/your-org/home-panel:latest
region: home
exposure: tailscale-relay
domain: panel.example.com
port: 8080
publishPort: 8080
replicas: 1
relay:
  host: home-1.your-tailnet.ts.net
```

Luma generates:

- `stacks/home/home-panel/home-panel.nomad.json`, which publishes the service port on the home node;
- `routes/home-panel.yml`, which Traefik loads through the file provider;
- Cloudflare DNS record pointing to the configured CN edge target.

Operational requirement:

- the home host firewall should only allow the published service port from the Tailscale interface or from the CN edge Tailscale IP;
- `routes/` must be available on the CN Traefik node at `/opt/luma/routes`.

### `tcp-relay`

Public native TCP services through a dedicated Traefik TCP entrypoint.

```text
Client -> Cloudflare DNS -> edge Traefik TCP entrypoint -> task host port
```

Use for:

- MySQL or another non-HTTP protocol that must be reachable from the public internet;
- a service where one public port maps to one backend service.

Do not use for:

- multiplexing multiple ordinary MySQL services on the same port by hostname;
- public exposure without database credentials, IP allowlists, or firewall controls.

Manifest:

```yaml
name: ledger-db
image: mysql:8.4.9
region: home
node: lab
exposure: tcp-relay
domain: ledger-db.example.net
port: 3306
publishPort: 3306
```

Luma generates:

- `stacks/home/ledger-db/ledger-db.nomad.json`, which publishes the service port in host mode;
- Traefik service update for the derived `tcp-3306` entrypoint and host-mode published port;
- `routes/ledger-db.yml`, which uses Traefik `tcp.routers` with `HostSNI("*")` / `HostSNI(\`*\`)`;
- Cloudflare DNS record pointing to the configured edge target.

Ordinary MySQL clients do not start with an HTTP Host header, and should not be assumed to provide reliable TLS SNI before the server handshake. Treat `tcp-relay` as port-exclusive: one published port routes to one TCP service.

### `cloudflare-tunnel`

Home or private services exposed directly through Cloudflare Tunnel.

```text
User -> Cloudflare -> cloudflared -> service
```

Use for:

- home services without a public IP;
- low-frequency tools where you prefer Cloudflare to terminate the public path;
- services that should not route through the CN Traefik node.

Manifest:

```yaml
name: home-tool
image: ghcr.io/your-org/home-tool:latest
region: home
exposure: cloudflare-tunnel
domain: tool.example.com
port: 8080
replicas: 1
tunnel:
  tokenEnv: CLOUDFLARE_TUNNEL_TOKEN
```

Luma generates:

- a service stack containing the app;
- a `cloudflared` sidecar service using `${CLOUDFLARE_TUNNEL_TOKEN}`.

Cloudflare Tunnel public hostname configuration is still managed in Cloudflare. Luma skips normal DNS A-record sync for this mode.

### `external-edge`

Global services with their own overseas/public edge.

```text
User -> Cloudflare DNS -> external/global edge -> global service
```

Use for:

- AI gateway;
- overseas API gateway;
- proxy services;
- low-frequency services that must execute outside the CN network.

Manifest:

```yaml
name: ai-gateway
image: ghcr.io/your-org/ai-gateway:latest
region: global
exposure: external-edge
domain: ai.example.com
port: 3000
replicas: 1
dns:
  target: 198.51.100.10
```

Luma generates:

- `stacks/global/ai-gateway/ai-gateway.nomad.json`;
- Traefik service tags for the global edge environment;
- Cloudflare DNS record pointing to `dns.target`.

Use `dns.target` to choose the public/global edge IP for this service. Without a separate global edge target, a global workload can still be scheduled in `region: global`, but public HTTP traffic will not magically enter through every global node.

### `node-edge`

A node with its own public IP serves its services directly.

```text
User -> DNS -> node Traefik -> service on the same node
```

Use for:

- bandwidth-heavy services whose traffic should not cross, or be billed on, the Manager;
- a cloud server added to the cluster purely as a deployment target.

Not for:

- services that need more than one node (there is no failover between nodes);
- home or macOS nodes.

Enable ingress on the node once, then deploy to it:

```bash
luma node ingress enable tx-sh-1 --public-ip 203.0.113.20
```

```yaml
name: downloads
image: ghcr.io/your-org/downloads:latest
region: cn
node: tx-sh-1
exposure: node-edge
domain: dl.example.com
port: 8080
replicas: 2
```

Luma generates:

- a `traefik-node-tx-sh-1` Nomad job bound to ports 80 and 443 on that node only;
- Nomad service tags for the app with `luma.edge=tx-sh-1`, which the Manager's Traefik ignores;
- a DNS record pointing at the node's public IP, in whichever [DNS zone](dns-zones.md) owns the domain.

The node's Traefik issues certificates over HTTP-01 and holds no DNS credentials. `dns.target` is not accepted: the record always follows the node's address. Rerun `luma node ingress enable` to re-check the ports or change the address; Luma then repoints the DNS of every `node-edge` service on the node.

Operational requirements:

- the cloud security group and host firewall allow inbound TCP 80 and 443;
- in mainland China the domain needs ICP filing at the node's cloud provider, or the provider blocks the ports;
- `luma node ingress disable` is refused while any `node-edge` deployment still uses the node;
- not yet available once the cluster has the Nomad security profile (ACL and mTLS): `luma node ingress enable` is refused there, so keep those domains on `cn-edge` or `external-edge`.

### `none`

Internal services and workers.

```text
No public route
```

Use for:

- workers;
- queue consumers;
- internal jobs;
- backup tasks.

Manifest:

```yaml
name: fetch-worker
image: ghcr.io/your-org/fetch-worker:latest
region: global
exposure: none
replicas: 1
env:
  QUEUE_URL: redis://redis:6379/0
```

Luma generates:

- a stack with placement constraints;
- no Traefik labels;
- no DNS record.

## Decision rule

- Default public product traffic: `cn-edge`.
- Home service through the domestic edge: `tailscale-relay`.
- Public native TCP service: `tcp-relay`.
- Home/private service through Cloudflare: `cloudflare-tunnel`.
- Overseas public execution: `external-edge`.
- Public traffic served by a node with its own public IP, bypassing the Manager: `node-edge`.
- Workers and internal services: `none`.
