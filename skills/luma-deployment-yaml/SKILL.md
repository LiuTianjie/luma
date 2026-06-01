---
name: luma-deployment-yaml
description: Generate and review Luma deployment YAML service manifests for the infra-stacks/Luma project. Use when asked to create, validate, explain, or fix luma deploy YAML files, choose region/exposure, route a domain to a workload, or prepare examples for cn-edge, external-edge, tailscale-relay, cloudflare-tunnel, global worker, or home internal services.
---

# Luma Deployment YAML

Use this skill to create Luma service manifests consumed by `luma deploy`.

Luma manifests are not Docker Compose. They describe one service: image, region, optional node pin, exposure, domain, port, replicas, and a few runtime options. Luma renders Swarm stacks, DNS, Traefik routes, and Portainer deployment actions.

## Workflow

1. Identify the service type:
   - public China service: `region: cn`, `exposure: cn-edge`
   - public global service: `region: global`, `exposure: external-edge`
   - home service through Tailscale relay: `region: home`, `exposure: tailscale-relay`
   - Cloudflare Tunnel service: usually `region: home`, `exposure: cloudflare-tunnel`
   - worker/internal service: `exposure: none`
   - runtime proxy service: add `proxy: true` when the container itself must access external networks through Luma egress
2. Ask only for missing required facts: service name, image, domain, container port, region/exposure, Luma node name when pinning is required, and relay/tunnel details when needed.
3. Emit only the manifest YAML unless the user asks for explanation.
4. Prefer `${ENV_NAME}` references for secrets; do not put plaintext secrets in YAML.
5. Recommend validation with `luma validate <file>` and `luma deploy <file> --dry-run`.

## Hard Rules

- `name`, `image`, and `region` are required.
- Valid regions: `cn`, `global`, `home`.
- `node` is optional and pins the service to the Luma node name passed to `luma node join --name`. During deploy, Luma resolves that name to the real Swarm NodeID and renders a `node.labels.luma.node.id == <node-id>` constraint. Use it only for stateful, home, or debugging workloads that must run on one machine.
- Valid exposures: `none`, `cn-edge`, `external-edge`, `tailscale-relay`, `cloudflare-tunnel`.
- Public exposures require `domain` and integer `port`.
- `cn-edge` must use `region: cn`.
- `external-edge` must use `region: global`.
- `tailscale-relay` must use `region: home`. `relay.host`/`relay.url` are optional advanced overrides; by default Luma Control infers upstreams from the Swarm tasks' actual home nodes after deploy.
- `replicas` defaults to `1`; when present it must be `>= 1`.
- `port` is the container's internal listening port, not the cloud firewall/security-group port.
- Avoid the legacy `public` field in new files. If present, it must match `exposure != none`.
- Use `proxy: true` for runtime outbound proxy needs. Do not hand-write the default `HTTP_PROXY`, `HTTPS_PROXY`, or `egress` network just to use Luma egress.
- `proxy: true` is not for image pulls. Image pulls use the Docker daemon proxy configured by egress setup.
- `proxy: true` can be combined with `region: home` and `exposure: tailscale-relay`: inbound traffic uses the relay path, while the container's outbound HTTP/HTTPS traffic uses Luma egress.
- Plain env values may be written directly under `env`.
- Secret env values must use `${NAME}` and be stored with `luma secret set NAME` before deployment.

## Minimal Templates

China public service:

```yaml
name: api
image: ghcr.io/acme/api:1.0.0
region: cn
exposure: cn-edge
domain: api.example.com
port: 3000
replicas: 2
```

Global worker:

```yaml
name: fetch-worker
image: ghcr.io/acme/fetch-worker:1.0.0
region: global
exposure: none
replicas: 1
env:
  QUEUE_URL: redis://redis:6379/0
  OPENAI_API_KEY: ${OPENAI_API_KEY}
```

Pinned home worker:

```yaml
name: home-db
image: postgres:16
region: home
node: home-mac-mini
exposure: none
volumes:
  - home_db_data:/var/lib/postgresql/data
```

Worker that needs Luma egress proxy:

```yaml
name: ai-worker
image: ghcr.io/acme/ai-worker:1.0.0
region: cn
exposure: none
proxy: true
env:
  OPENAI_BASE_URL: https://api.openai.com/v1
  OPENAI_API_KEY: ${OPENAI_API_KEY}
```

Luma renders the `egress` overlay network and default `HTTP_PROXY` / `HTTPS_PROXY` automatically. Scheduling still follows `region`. Existing proxy env vars in `env` are preserved.

When emitting `${NAME}` values, also tell the user to run:

```bash
luma secret set NAME
```

Home Tailscale relay:

```yaml
name: home-panel
image: ghcr.io/acme/home-panel:1.0.0
region: home
exposure: tailscale-relay
domain: panel.example.com
port: 8080
publishPort: 8080
replicas: 1
```

Luma Control infers the relay upstream from the actual running Swarm task after deploy. Set `node` only when the service must run on one specific machine; set `relay.url` only for advanced manual override.

Cloudflare Tunnel:

```yaml
name: home-tool
image: ghcr.io/acme/home-tool:1.0.0
region: home
exposure: cloudflare-tunnel
domain: tool.example.com
port: 8080
replicas: 1
tunnel:
  tokenEnv: CLOUDFLARE_TUNNEL_TOKEN
```

## Additional Reference

For the full field table and more examples, read `references/manifest-reference.md`.
