# Exposure Model

Luma separates the control plane from the data plane.

## Roles

- Cloudflare: DNS automation, optional proxy, optional Tunnel public hostname.
- Traefik: main public HTTP/HTTPS ingress for the `cn` edge, plus `tcp-relay` published ports.
- Tailscale: management network and explicit relay path for selected `home` services.
- Portainer: deployment control plane.
- Docker Swarm / Docker: runtime execution layer.

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

- `stacks/cn/app/stack.yml`;
- Traefik Swarm labels;
- Cloudflare DNS record pointing to the configured CN edge target.

By default, public traffic enters through one Traefik replica constrained to `node.labels.region == cn` and `node.labels.ingress == true`. If you have multiple CN nodes, DNS still points to the configured edge target; Traefik receives the request there and forwards to matching service tasks over the Swarm overlay network.

### `tailscale-relay`

Selected home services exposed through the CN edge over Tailscale.

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

- `stacks/home/home-panel/stack.yml`, which publishes the service port on the home node;
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
name: granary-db
image: mysql:8.4.9
region: home
node: lab
exposure: tcp-relay
domain: granary-db.itool.tech
port: 3306
publishPort: 3306
```

Luma generates:

- `stacks/home/granary-db/stack.yml`, which publishes the service port in host mode;
- Traefik service update for the derived `tcp-3306` entrypoint and host-mode published port;
- `routes/granary-db.yml`, which uses Traefik `tcp.routers` with `HostSNI("*")` / `HostSNI(\`*\`)`;
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

- `stacks/global/ai-gateway/stack.yml`;
- Traefik Swarm labels for the global edge environment;
- Cloudflare DNS record pointing to `dns.target`.

Use `dns.target` to choose the public/global edge IP for this service. Without a separate global edge target, a global workload can still be scheduled in `region: global`, but public HTTP traffic will not magically enter through every global node.

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
- Workers and internal services: `none`.
