# Egress Gateway

Egress Gateway is Luma's outbound proxy plane.

It solves two practical problems:

- domestic servers may fail to pull fresh images or dependencies directly;
- some services must access external networks while the public entrypoint remains domestic.

Egress is not an ingress path. Public user traffic still enters through the selected `exposure` mode.

## Setup

```bash
export EGRESS_SUBSCRIPTION_URL='...'
luma egress setup
```

Luma will:

- download the subscription;
- convert YAML or base64 subscription output into a minimal Mihomo config;
- write `/opt/luma/egress-gateway/config.yaml` with mode `600`;
- configure stable system DNS resolvers for registry/bootstrap reliability;
- temporarily disable Docker daemon proxy for the first egress bootstrap;
- deploy `stacks/core/egress-gateway/stack.yml` using Luma's built-in egress image;
- label the node as `egress=true`;
- configure Docker daemon proxy to `127.0.0.1:7890`;
- restart Docker.

The default egress image is a domestic registry mirror tested for first bootstrap:

```yaml
defaults:
  images:
    egressGateway: docker.1panel.live/metacubex/mihomo:latest
```

Most users do not need to configure an image. Advanced users can override `defaults.images.egressGateway` in `luma.yaml` when operating their own registry mirror.

Refresh later:

```bash
luma egress refresh
```

## Runtime

The gateway listens on:

```text
127.0.0.1:7890
```

The stack attaches to the `egress` overlay network. The firewall should block public inbound access to `7890`.

Docker daemon proxy:

```text
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
NO_PROXY=localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
```

## Verify

```bash
sudo docker service ls | grep egress
sudo docker pull hello-world:latest
```

From inside a service that joins the `egress` network, use:

```yaml
env:
  HTTP_PROXY: http://egress_mihomo:7890
  HTTPS_PROXY: http://egress_mihomo:7890
networks:
  - egress
```

## Security

- Do not commit `EGRESS_SUBSCRIPTION_URL`.
- Rotate subscription URLs that appear in chat, logs, or screenshots.
- Keep inbound `7890` blocked.
- Prefer one explicit egress node first. Add more only when scheduling or throughput requires it.
