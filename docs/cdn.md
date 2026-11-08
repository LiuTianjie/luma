# CDN

Luma can put a service behind Alibaba Cloud ESA (Edge Security Acceleration). One line in the manifest enables it; Luma then manages the ESA record and its rules for the life of the service. Services without `cdn` are unchanged and never touch Alibaba Cloud.

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

The variable names carry a `LUMA_` prefix so they never clash with Alibaba Cloud keys your applications use. `luma cdn check` without a domain verifies the key, the ESA plan, the Cloudflare zone and the ESA site.

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
  cache:                        # paths that are the same for every visitor
    - path: /blog/*
      ttl: 300
    - path: /opengraph-image*
      ttl: 86400
```

List under `cache` only paths whose response never depends on who is signed in. A response the application marks `private` or `no-store` is still not cached.

## What a deploy does

1. **Prepare CDN**, before DNS changes. The first time for a Luma zone, Luma creates the ESA site for the zone with CNAME access, proves ownership with the `_esaauth` TXT record in Cloudflare and applies the site-wide settings below. For each service it creates a proxied ESA record pointing at the edge, updates it when the edge address changed, and reconciles the rules for its `cache` paths. A failure here stops the deploy before anything has changed.
2. **Sync DNS** points the domain at the ESA CNAME. Switching between A and CNAME replaces the record in place.
3. **ESA certificate** requests ESA's free certificate and waits for it, up to 10 minutes. ESA validates the certificate over HTTP, so this can only happen after DNS points at ESA; a domain switching for the first time may have no HTTPS certificate for a few minutes. If it takes longer, the step is a warning and the rollout continues. ESA renews the certificate automatically.
4. The Nomad rollout and the public route probe run as before.
5. **Purge CDN** refreshes the cache after the new version is live: the whole hostname by default, or the `purge` paths. A refresh failure is a warning, never a failed deploy; run `luma cdn purge` to retry. A newly created record is not refreshed because it has no cache yet.

Site-wide settings, shared by every Luma domain on the ESA site. They are global configs, which cost no rule quota:

- **Origin**: fetch with the client's protocol and the request's `Host`, and verify Traefik's certificate. Plain HTTP gets Traefik's redirect to HTTPS.
- **Cache**: honour the application's cache headers on every path, HTML included. Without headers, ESA's defaults apply: common static files (js, css, images, fonts and similar) are cached, everything else is not.
- Gzip and Brotli compression, and the `ali-real-client-ip` header with the real client address.

Each `cache` path gets one rule for its domain: honour the application's headers, and without them cache for `ttl` seconds. A matching rule replaces the global setting, and `cache` paths cannot overlap, so rule order never matters. Rules count against a per-site quota shared by every application on the root domain: 5 on Free, 10 on Basic, 50 on Standard. A deploy that would exceed it fails at Prepare CDN with that explanation.

ESA defaults cover the rest: HTTP/2 and HTTP/3, TLS 1.0 off, basic DDoS protection and the managed WAF rules.

## Cache as much as possible

Without its own configuration ESA caches only static files: HTML and paths without an extension are never cached, whatever the application returns. Luma's site-wide cache setting makes the application's headers count on every path, so caching more comes down to two levers:

- Return cache headers from the application:

```text
/_next/static/*, /assets/*.<hash>.js   Cache-Control: public, max-age=31536000, immutable
HTML pages                              Cache-Control: public, max-age=0, s-maxage=60
API, signed-in pages                    Cache-Control: private, no-store
```

- Or list the paths that are the same for every visitor under `cdn.cache`, without changing the application.

Audit a domain to see where you stand. The check is read-only: it fetches the home page, the `purge` and `cache` paths, a few assets referenced by the home page, `/sitemap.xml` and `/robots.txt`, twice each, anonymously:

```bash
luma cdn check www.example.com
luma cdn check www.example.com --path /pricing --format json
```

- **DANGER**: a cacheable response that sets a cookie, or an account-like path marked public. Fix these first; the command exits non-zero.
- **IMPROVE**: a page without cache headers, a page revalidated on every request, a static file without cache headers, or large text sent uncompressed.
- **OK**: cacheable (and served from the edge when the second request hit), or explicitly not cached.

Files with a content hash in the name never need a refresh: a new version has a new URL. The default refresh after a deploy covers the whole hostname and only fetches changed resources again. ESA limits hostname and directory refreshes to about 100 per day per plan; URL refreshes allow far more. Applications that deploy many times a day should list fixed URLs in `purge`.

## Operations

```bash
luma cdn status
luma cdn purge www.example.com
luma cdn purge www.example.com / /pricing
luma cdn remove www.example.com
```

- `status` lists each domain with its site, CNAME, last refresh and last error.
- `purge` refreshes the whole hostname, or the given paths.
- `remove` deletes an ESA record and its `cache` rules once DNS no longer points at it.

To turn ESA off for a service, remove `cdn` from the manifest and deploy. DNS points at the edge again and the ESA record stays, so clients with a cached DNS answer keep working; delete it later with `luma cdn remove`. Removing the application deletes its ESA record and rules. The ESA site stays.
