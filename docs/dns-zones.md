# DNS Zones

Luma writes the DNS record of every public service. The record goes to the zone that owns the domain, so one cluster can serve domains hosted on different providers.

| Provider | `--provider` | Credentials |
|---|---|---|
| Cloudflare | `cloudflare` | API token with Zone → DNS → Edit |
| Alibaba Cloud DNS | `alidns` | AccessKey ID and Secret of a RAM user with AliyunDNSFullAccess |
| Tencent Cloud DNSPod | `dnspod` | SecretId and SecretKey of a CAM user with QcloudDNSPodFullAccess |

## The primary zone

The Cloudflare zone configured during first install (`luma manager cloudflare --zone`) is the primary zone. It also serves the Control domain, ESA verification records and Cloudflare Tunnel. A hostname outside every added zone keeps using the primary zone, so existing clusters behave as before.

## Add a zone

```bash
luma dns zone add example.cn --provider alidns
```

The CLI prompts for each credential without echoing it. In CI it reads the variables each vendor's own tools use instead:

- Cloudflare: `CLOUDFLARE_API_TOKEN`;
- Alibaba Cloud: `ALIBABA_CLOUD_ACCESS_KEY_ID`, `ALIBABA_CLOUD_ACCESS_KEY_SECRET`;
- Tencent Cloud: `TENCENTCLOUD_SECRET_ID`, `TENCENTCLOUD_SECRET_KEY`.

Control calls the provider once before saving. A key that cannot read the zone is rejected and nothing is stored. Saved keys are Control secrets named `LUMA_DNS_<ZONE>_<FIELD>`; the API never returns them.

A second domain on the same account reuses the key:

```bash
luma dns zone add example.com.cn --provider alidns --same-as example.cn
```

The console offers the same steps under **Infrastructure → Domains**.

## Use a zone

Manifests do not change. A service with `domain: api.example.cn` publishes its record in the `example.cn` zone on the next deploy. When two zones match, the longer one wins: `api.shop.example.cn` uses `shop.example.cn` if both are listed.

```yaml
name: api
image: ghcr.io/your-org/api:latest
region: cn
exposure: cn-edge
domain: api.example.cn
port: 8080
```

Differences outside Cloudflare:

- `dns.proxied` is a Cloudflare feature and is rejected for other providers;
- both Chinese providers require a TTL of at least 600 seconds on their free plans, so Luma raises lower values;
- records are written on the default line;
- [CDN](cdn.md) currently supports domains in the primary zone only.

## Certificates

The Manager's Traefik validates the primary zone with Cloudflare DNS-01 and keeps its wildcard certificate. Hostnames in an Alibaba Cloud or DNSPod zone use a second resolver that validates over HTTP-01 on port 80, so their DNS record must point at the edge before the certificate is issued. Luma writes DNS before the rollout, so the first request after deploy can take a few seconds while the certificate is issued.

[`node-edge`](exposure-model.md#node-edge) services always use HTTP-01, whichever zone owns the domain.

## Check, rotate and remove

```bash
luma dns zone list
luma dns zone check example.cn
luma dns zone add example.cn --provider alidns --update
luma dns zone remove example.cn
```

`check` only reads from the provider. `--update` replaces the stored key; fields left empty keep their current value. `remove` is refused while a deployment still has a domain or `redirectFrom` host in the zone, and it leaves the records at the provider untouched.
