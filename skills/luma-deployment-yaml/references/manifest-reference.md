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
| `externalNet` | no | boolean | For `global + exposure:none`, defaults to true and adds `node.labels.external_net == true`. |
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

## Render Behavior

- `cn-edge` and `external-edge` generate Traefik labels:
  - `traefik.enable=true`
  - `Host(<domain>)`
  - configured entrypoint
  - configured ACME cert resolver
  - load balancer server port from `port`
- Public Traefik services are attached to the configured public overlay network.
- Every service gets `node.labels.region == <region>`.
- `global + exposure:none + externalNet:true` also gets `node.labels.external_net == true`.
- `tailscale-relay` creates a host-mode published port and a file-provider Traefik route to the relay upstream.
- `cloudflare-tunnel` adds a `cloudflared` sidecar service using `${<tokenEnv>}`.

## Review Checklist

- Does `domain` match the actual user-facing hostname?
- Is `port` the container port, not the public firewall port?
- Is `region` compatible with `exposure`?
- Are secrets represented as `${ENV_NAME}` instead of plaintext?
- For every `${ENV_NAME}`, remind the user to run `luma secret set ENV_NAME` before deploying.
- Does the image include a meaningful tag?
- Should this be public at all, or is `exposure: none` safer?
- For home services, is latency/availability acceptable for the workload?

## Validation Commands

```bash
luma validate service.yaml
luma deploy service.yaml --dry-run
```
