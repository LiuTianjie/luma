# CDN (Alibaba Cloud ESA)

Use this when a user wants a Luma service accelerated, cached, or put behind a
CDN, when `cdn:` appears in a manifest, or when a deploy fails or warns at
`Prepare CDN` or `Purge CDN`. Product documentation:
`docs/cdn.md`.

## Model

- `cdn: esa` is opt-in per service. A service without it keeps an A record
  straight to Traefik and never touches Alibaba Cloud.
- One ESA site per Luma zone (root domain), joined by CNAME. One ESA plan
  serves one site. Luma never buys plans; the user buys one in the console.
- DNS stays in Cloudflare: Luma writes a DNS-only CNAME to the ESA record's
  `RecordCname`. Never suggest turning on the Cloudflare proxy for these names.
- Site-wide settings: origin always HTTPS with certificate verification, edge
  force-HTTPS, one cache rule (`cache_all` + `follow_origin_bypass`) honouring
  app headers on every path, Gzip/Brotli/Zstd, HTTP/2+HTTP/3, HTTP/2 to origin,
  WebSocket, and the `ali-real-client-ip` header. Paid features (smart
  routing) stay off.
- Prepare CDN writes `_acme-challenge.<domain>` CNAME (DCV delegation) and waits
  until a certificate whose SAN covers the domain exists and the ESA edge
  completes a TLS handshake for it, before DNS moves, so there is no HTTPS gap.
  (ESA lists other site certificates for a record; never trust its count.) Keep that
  record; renewals use it.
- Each `cdn.cache` path is one cache rule on the site. The per-site quota is
  shared by every app on the root domain: Free 5, Basic 10, Standard 50.
- Only `cn-edge`, `external-edge` and `tailscale-relay` accept `cdn`.
- Never put `cdn` on the zone apex (Luma refuses it): Cloudflare flattens apex
  CNAMEs, so ESA schedules visitors by Cloudflare's location (China visitors got
  US nodes, 10 s pages). Use `domain: www.<zone>` plus `redirectFrom: [<zone>]`;
  the apex stays a direct A record, GET/HEAD 301 to www, other methods pass
  through (webhooks keep working). If other products call apex APIs with an
  `Authorization` header, use `redirectFrom: {hosts: [<zone>], except: ["/api/*"]}`:
  clients drop that header on cross-host redirects. App side: canonical/sitemap/OAuth on www,
  keep the WebAuthn RP ID at the root domain so passkeys survive.
- One site-wide cache rule (`luma_site_origin_headers`, `Rule: true`,
  `BypassCache: cache_all`, `follow_origin_bypass`, Sequence 1) makes HTML
  honour app headers; without `cache_all` ESA reports HTML as `DYNAMIC`.

## Manifest

```yaml
cdn: esa
```

```yaml
cdn:
  provider: esa                 # only esa today
  scope: global                 # global (default, needs ICP) | domestic (needs ICP) | overseas
  purge: ["/", "/sitemap.xml"]  # omit: whole hostname; []: none; "/blog/*": directory
  cache:                        # only paths identical for every visitor
    - path: /blog/*
      ttl: 300
```

Compose: put the same block under `services.<name>.cdn` in the sidecar.

Rules for agents:

- Never put signed-in, per-user, cart, account or API paths in `cdn.cache`.
- `cache` paths start with `/`, may only end with `*`, must not overlap, at
  most 8 per service, `ttl` 1 to 31536000 seconds.
- Prefer fixed URLs in `purge` for apps that deploy many times a day: hostname
  and directory refreshes are limited to about 100 per day per plan.

## Setup, in order

1. User buys an ESA plan (Basic is enough for most sites).
2. User creates a RAM user with programmatic access and the `esa:*` policy.
3. Store its key in Control (prefixed names, so app secrets never clash):

```bash
luma secret set LUMA_ALIBABA_CLOUD_ACCESS_KEY_ID
luma secret set LUMA_ALIBABA_CLOUD_ACCESS_KEY_SECRET
luma cdn check
```

4. Run `luma cdn check DOMAIN` and fix any DANGER row, then add `cdn: esa`
   and deploy. The first deploy for a domain waits in Prepare CDN until the
   certificate is issued (usually a few minutes); traffic switches only after.

## Before enabling on a real app: audit caching

```bash
luma cdn check www.example.com
luma cdn check www.example.com --path /pricing --format json
```

Read-only: fetches the home page, `purge` and `cache` paths, assets the home
page references, `/sitemap.xml` and `/robots.txt`, twice each, anonymously.

- `DANGER`: cacheable response with `Set-Cookie`, or an account-like path marked
  public. Fix in the app first (`Cache-Control: private, no-store`); the command
  exits 1.
- `IMPROVE`: page without cache headers, page with `max-age=0`, static file
  without cache headers, or large uncompressed text. Either add
  `Cache-Control: public, s-maxage=...` in the app or, if the URL is the same
  for everyone, list it in `cdn.cache`. Uncompressed text is fixed by ESA's
  compression once enabled.
- `OK`: cacheable (edge hit on the second request), or explicitly not cached.

Recommended app headers:

```text
/_next/static/*, /assets/*.<hash>.js   Cache-Control: public, max-age=31536000, immutable
HTML pages                              Cache-Control: public, max-age=0, s-maxage=60
API, signed-in pages                    Cache-Control: private, no-store
```

## Deploy steps and failures

| Step | When it fails | What to do |
|---|---|---|
| `Check component prerequisites` (`cdn`) | `LUMA_ALIBABA_CLOUD_ACCESS_KEY_*` missing | Set both secrets. |
| `Prepare CDN` | `no ESA plan with a free site slot` | User buys a plan; one plan per root domain. |
| `Prepare CDN` | `InvalidSiteICP` | Use `scope: overseas` or finish ICP filing. |
| `Prepare CDN` | `ownership check did not pass` | Confirm `_esaauth.<zone>` TXT in Cloudflare holds the verify code; redeploy. |
| `Prepare CDN` | `quota reached for this site` | Remove `cdn.cache` paths across apps or upgrade the plan. |
| `Prepare CDN` | `uses NS access` | Luma only manages CNAME-access sites. |
| `Prepare CDN` | `cannot serve the zone apex` | Move to `domain: www.<zone>` + `redirectFrom: [<zone>]`. |
| `x-site-cache-status: DYNAMIC` on HTML | site rule missing or app sends no-store | Redeploy (recreates the site rule); check app headers with `luma cdn check DOMAIN`. |
| `Prepare CDN` | `certificate ... still being issued` | DNS was not changed. Check `_acme-challenge.<domain>` CNAME in Cloudflare, wait a few minutes, deploy again. |
| `Purge CDN` (warning) | quota or API error | Deploy is fine; `luma cdn purge DOMAIN` to retry. |

Nothing in `Prepare CDN` changes DNS, so a failure there leaves the service as
it was. Writes are not retried after timeouts; the next deploy reconciles.

## Operations

```bash
luma cdn status
luma cdn purge www.example.com
luma cdn purge www.example.com / /pricing
luma cdn remove www.example.com
```

- Turn off: remove `cdn` and deploy. DNS returns to the A record; the ESA
  record stays until `luma cdn remove DOMAIN` (refused while DNS still points
  at ESA).
- Removing the app deletes its ESA record and `cache` rules; the site stays.
- Real client IP: Traefik access logs keep `Ali-Real-Client-Ip`; apps that need
  the visitor IP read that header.
