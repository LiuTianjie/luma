# Manifest Reference

## Fields

| Field | Required | Type | Notes |
| --- | --- | --- | --- |
| `name` | yes | string | Service name; Luma slugifies it for stack/service/router names. |
| `image` | yes | string | Container image. Prefer pinned tags. |
| `region` | yes | `cn` / `global` / `home` | Runtime placement region. |
| `exposure` | recommended | `none` / `cn-edge` / `external-edge` / `tailscale-relay` / `cloudflare-tunnel` | Access mode. Use explicit exposure in new files. |
| `domain` | public only | string | Public hostname. |
| `port` | public only | integer | Container internal port. |
| `replicas` | no | integer | Defaults to `1`. Must be at least `1`. |
| `env` or `environment` | no | map | Service environment variables. Use direct values for non-sensitive settings and `${SECRET_NAME}` for secrets stored in Luma Control. |
| `command` | no | string/list | Overrides container command. |
| `constraints` | no | string[] | Extra Swarm placement constraints. Luma adds region constraints. |
| `labels` | no | string[] | Extra service labels. Luma adds Traefik labels for `cn-edge` and `external-edge`. |
| `networks` | no | string[] | Extra external overlay networks. |
| `proxy` | no | boolean | Runtime proxy requirement. When true, Luma adds the egress network and default proxy env. Scheduling still follows `region`. This is not for image pulls. |
| `publishPort` | relay only | integer | Host mode published port for tailscale relay. |
| `relay.host` | tailscale-relay | string | Tailscale hostname. Alternative: `relay.url`. |
| `relay.url` | tailscale-relay | string | Full upstream URL. Alternative: `relay.host`. |
| `tunnel.tokenEnv` | cloudflare-tunnel | string | Env var name for Cloudflare tunnel token. Defaults to `CLOUDFLARE_TUNNEL_TOKEN`. |
| `dns` | no | map | Reserved for DNS extensions. |
| `portainer` | no | map | Reserved for Portainer integration extensions. |
| `stackPath` | no | string | Override generated stack path. Rarely needed. |
| `routePath` | no | string | Override generated tailscale route path. Rarely needed. |

## Exposure Matrix

| Goal | YAML |
| --- | --- |
| Domestic public HTTPS service | `region: cn`, `exposure: cn-edge`, `domain`, `port` |
| Overseas/global HTTPS service | `region: global`, `exposure: external-edge`, `domain`, `port` |
| Home service through China edge and Tailscale | `region: home`, `exposure: tailscale-relay`, `domain`, `port`, `relay.host` or `relay.url` |
| Home/private service through Cloudflare Tunnel | `region: home`, `exposure: cloudflare-tunnel`, `domain`, `port`, `tunnel.tokenEnv` |
| Queue worker or internal service | `exposure: none`, no `domain`, no `port` required |
| Service runtime needs Luma egress proxy | add `proxy: true`; keep `region` as the desired scheduling region |

## Render Behavior

- `cn-edge` and `external-edge` generate Traefik labels:
  - `traefik.enable=true`
  - `Host(<domain>)`
  - configured entrypoint
  - configured ACME cert resolver
  - load balancer server port from `port`
- Public Traefik services are attached to the configured public overlay network.
- Every service gets `node.labels.region == <region>`.
- `proxy: true` services also get the configured egress overlay network and default `HTTP_PROXY=http://egress_mihomo:7890` / `HTTPS_PROXY=http://egress_mihomo:7890` env values unless already set. Scheduling still follows `region`.
- `tailscale-relay` creates a host-mode published port and a file-provider Traefik route to the relay upstream.
- `cloudflare-tunnel` adds a `cloudflared` sidecar service using `${<tokenEnv>}`.

## Proxy Example

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

Do not add the default `egress` network or default proxy env manually for this case. Add custom `HTTP_PROXY` or `HTTPS_PROXY` only when overriding the default proxy target.

## Review Checklist

- Does `domain` match the actual user-facing hostname?
- Is `port` the container port, not the public firewall port?
- Is `region` compatible with `exposure`?
- Are secrets represented as `${ENV_NAME}` instead of plaintext?
- For every `${ENV_NAME}`, remind the user to run `luma secret set ENV_NAME` before deploying.
- Does the image include a meaningful tag?
- If the service needs external runtime network access, did you use `proxy: true` instead of manual egress network/env boilerplate?
- Should this be public at all, or is `exposure: none` safer?
- For home services, is latency/availability acceptable for the workload?

## Validation Commands

```bash
luma validate service.yaml
luma deploy service.yaml --dry-run
```
