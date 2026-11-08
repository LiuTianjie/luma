# CDN

Luma can put a service behind Alibaba Cloud ESA (Edge Security Acceleration). One line in the manifest enables it; Luma then manages the ESA record for the life of the service. Services without `cdn` are unchanged and never touch Alibaba Cloud.

```text
User -> Alibaba Cloud ESA node -> (cache miss) -> CN Traefik -> service
```

## When to use it

- Public HTTP services on `cn-edge`, `external-edge` or `tailscale-relay`. Other exposures are rejected.
- Domains with an ICP filing use `scope: global`: one CNAME serves users in mainland China and abroad. `scope: overseas` needs no filing.
- DNS stays in Cloudflare. Luma writes a DNS-only CNAME to ESA; do not turn on the Cloudflare proxy for these names.

## One-time setup

1. Buy an ESA plan in the Alibaba Cloud console. One plan serves one root domain (one ESA site). Luma never buys plans.
2. Create a RAM user with programmatic access only and attach this policy, which grants ESA and nothing else:

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "esa:*",
      "Resource": "*"
    }
  ]
}
```

3. Store the RAM user's AccessKey in Luma Control and check the setup:

```bash
luma secret set LUMA_ALIBABA_CLOUD_ACCESS_KEY_ID
luma secret set LUMA_ALIBABA_CLOUD_ACCESS_KEY_SECRET
luma cdn check
```

The variable names carry a `LUMA_` prefix so they never clash with Alibaba Cloud keys your applications use. `luma cdn check` verifies the key, the ESA plan, the Cloudflare zone and the ESA site.

## Enable it for a service

Add `cdn: esa` and deploy as usual:

```yaml
name: web
image: ghcr.io/example/web:1.4.0
region: cn
exposure: cn-edge
domain: www.example.com
port: 3000
cdn: esa
```

All fields:

```yaml
cdn:
  provider: esa                 # only esa today
  scope: global                 # global (default) | domestic | overseas
  purge: ["/", "/sitemap.xml"]  # omit: refresh the whole hostname; []: no refresh; "/blog/*": a directory
```

## What a deploy does

1. **Prepare CDN**, before DNS changes. The first time for a Luma zone, Luma creates the ESA site for the zone with CNAME access, proves ownership with the `_esaauth` TXT record in Cloudflare and applies the site defaults below. For each service it creates a proxied ESA record pointing at the edge, or updates it when the edge address changed. A failure here stops the deploy before anything has changed.
2. **Sync DNS** points the domain at the ESA CNAME. Switching between A and CNAME replaces the record in place.
3. **ESA certificate** requests ESA's free certificate and waits for it, up to 10 minutes. ESA validates the certificate over HTTP, so this can only happen after DNS points at ESA; a domain switching for the first time may have no HTTPS certificate for a few minutes. ESA renews it automatically.
4. The Nomad rollout and the public route probe run as before.
5. **Purge CDN** refreshes the cache after the new version is live: the whole hostname by default, or the `purge` paths. A refresh failure is a warning, never a failed deploy; run `luma cdn purge` to retry. A newly created record is not refreshed because it has no cache yet.

Site defaults Luma applies once per ESA site. An existing global setting of the same kind is left as it is.

- Back-to-origin protocol follows the client: HTTPS requests fetch over HTTPS, and plain HTTP gets Traefik's redirect to HTTPS.
- Gzip and Brotli compression.
- The `ali-real-client-ip` request header with the real client address. Traefik access logs keep it.

ESA defaults cover the rest: HTTP/2 and HTTP/3, TLS 1.0 off, basic DDoS protection and the managed WAF rules.

## Cache headers

The CDN saves traffic only for responses the application marks as cacheable. Before enabling it, check what the application returns:

```text
/_next/static/*, /assets/*.<hash>.js   Cache-Control: public, max-age=31536000, immutable
HTML pages                              Cache-Control: public, max-age=0, s-maxage=60
API, signed-in pages                    Cache-Control: private, no-store
```

Files with a content hash in the name never need a refresh: a new version has a new URL. Give every personalized or signed-in response `Cache-Control: private, no-store`.

The default refresh after a deploy covers the whole hostname and only fetches changed resources again. ESA limits hostname and directory refreshes to about 100 per day per plan; URL refreshes allow far more. Applications that deploy many times a day should list fixed URLs in `purge`.

Check a response through ESA:

```bash
curl -sI https://www.example.com/ | grep -iE 'cache-control|x-site-cache-status|via'
```

## Operations

```bash
luma cdn status
luma cdn purge www.example.com
luma cdn purge www.example.com / /pricing
luma cdn remove www.example.com
```

- `status` lists each domain with its site, CNAME, last refresh and last error.
- `purge` refreshes the whole hostname, or the given paths.
- `remove` deletes an ESA record once DNS no longer points at it.

To turn ESA off for a service, remove `cdn` from the manifest and deploy. DNS points at the edge again and the ESA record stays, so clients with a cached DNS answer keep working; delete it later with `luma cdn remove`. Removing the application deletes its ESA record. The ESA site stays.
