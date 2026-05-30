---
name: luma-deployment-yaml
description: Generate and review Luma deployment YAML service manifests for the infra-stacks/Luma project. Use when asked to create, validate, explain, or fix luma deploy YAML files, choose region/exposure, route a domain to a workload, or prepare examples for cn-edge, external-edge, tailscale-relay, cloudflare-tunnel, global worker, or home internal services.
---

# Luma Deployment YAML

Use this skill to create Luma service manifests consumed by `luma deploy`.

Luma manifests are not Docker Compose. They describe one service: image, region, exposure, domain, port, replicas, and a few runtime options. Luma renders Swarm stacks, DNS, Traefik routes, and Portainer deployment actions.

## Workflow

1. Identify the service type:
   - public China service: `region: cn`, `exposure: cn-edge`
   - public global service: `region: global`, `exposure: external-edge`
   - home service through Tailscale relay: `region: home`, `exposure: tailscale-relay`
   - Cloudflare Tunnel service: usually `region: home`, `exposure: cloudflare-tunnel`
   - worker/internal service: `exposure: none`
2. Ask only for missing required facts: service name, image, domain, container port, region/exposure, and relay/tunnel details when needed.
3. Emit only the manifest YAML unless the user asks for explanation.
4. Prefer `${ENV_NAME}` references for secrets; do not put plaintext secrets in YAML.
5. Recommend validation with `luma validate <file>` and `luma deploy <file> --dry-run`.

## Hard Rules

- `name`, `image`, and `region` are required.
- Valid regions: `cn`, `global`, `home`.
- Valid exposures: `none`, `cn-edge`, `external-edge`, `tailscale-relay`, `cloudflare-tunnel`.
- Public exposures require `domain` and integer `port`.
- `cn-edge` must use `region: cn`.
- `external-edge` must use `region: global`.
- `tailscale-relay` must use `region: home` and include `relay.host` or `relay.url`.
- `replicas` defaults to `1`; when present it must be `>= 1`.
- `port` is the container's internal listening port, not the cloud firewall/security-group port.
- Avoid the legacy `public` field in new files. If present, it must match `exposure != none`.
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
relay:
  host: home-1.your-tailnet.ts.net
```

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
