# Troubleshooting

Start with:

```bash
luma doctor
```

## Tailscale is not logged in

Create an ephemeral or reusable auth key in Tailscale, then:

```bash
export TAILSCALE_AUTHKEY='...'
luma tailscale connect <node>
```

## Docker image pulls fail

Fix:

```bash
export EGRESS_SUBSCRIPTION_URL='...'
luma egress setup <node>
luma doctor --deep
```

## Default deploy says Portainer webhook missing

Fix:

```bash
export PORTAINER_WEBHOOK_URL='...'
```

Or use emergency direct deploy:

```bash
luma deploy service.yaml --direct --node aly
```

## Cloudflare DNS fails

Use a Zone-scoped API token:

```text
Zone / DNS / Edit
Zone / Zone / Read
Specific zone: your domain
```

Then:

```bash
export CLOUDFLARE_API_TOKEN='...'
luma cloudflare connect --zone example.com
```

## Remote sudo fails

Either configure passwordless sudo for the SSH user or set:

```bash
export LUMA_SUDO_PASSWORD='...'
```

## Portainer is not reachable

Check:

```bash
docker stack services portainer
ufw status
```

The default Portainer HTTPS port is `9443`.

Repair:

```bash
luma portainer setup aly
```
