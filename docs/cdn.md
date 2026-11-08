# CDN

Luma can put a service behind Alibaba Cloud CDN. One line in the manifest enables it; Luma then manages the CDN domain for the life of the service.

```text
User -> Alibaba Cloud CDN node -> (cache miss) -> CN Traefik -> service
```

## When to use the CDN

- Public HTTP services on `cn-edge`, `external-edge` or `tailscale-relay`. Other exposures are rejected.
- Domains with an ICP filing use `scope: global`: one CNAME serves users in mainland China and abroad.
- Domains without an ICP filing, or overseas-only services, use `scope: overseas`, which needs no filing.
- Do not turn on the Cloudflare proxy (orange cloud) for these domains. DNS stays in Cloudflare as a DNS-only CNAME.

## One-time setup

1. Activate the CDN service in the Alibaba Cloud console. Do not add domains there; Luma adds them.
2. Create a RAM user with programmatic access only and attach this custom policy:

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "cdn:AddCdnDomain",
        "cdn:DeleteCdnDomain",
        "cdn:DescribeUserDomains",
        "cdn:DescribeCdnDomainConfigs",
        "cdn:BatchSetCdnDomainConfig",
        "cdn:SetCdnDomainSSLCertificate",
        "cdn:RefreshObjectCaches",
        "cdn:DescribeVerifyContent",
        "cdn:VerifyDomainOwner"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "yundun-cert:UploadUserCertificate",
        "yundun-cert:ListUserCertificateOrder"
      ],
      "Resource": "*"
    }
  ]
}
```

3. Store the RAM user's AccessKey in Luma Control and check the setup:

```bash
luma secret set ALIBABA_CLOUD_ACCESS_KEY_ID
luma secret set ALIBABA_CLOUD_ACCESS_KEY_SECRET
luma cdn check
```

`luma cdn check` verifies the key, the CDN and certificate APIs, the Traefik certificate store and the Cloudflare zone. The certificate store check needs a manager running this Luma version or later (`luma update`), because Control reads the Traefik certificate volume read-only.

## Enable the CDN for a service

Add `cdn: aliyun` and deploy as usual:

```yaml
name: web
image: ghcr.io/example/web:1.4.0
region: cn
exposure: cn-edge
domain: www.example.com
port: 3000
cdn: aliyun
```

All fields:

```yaml
cdn:
  provider: aliyun              # only aliyun today
  scope: global                 # global (default) | domestic | overseas
  purge: ["/", "/sitemap.xml"]  # refreshed after each deploy; default ["/"]; "/blog/*" refreshes a directory
```

The first deploy with `cdn` takes 3 to 10 extra minutes while Alibaba Cloud brings the domain online. Later deploys add a few seconds.

## What a deploy does

1. **Prepare CDN**, before DNS changes. Luma creates the accelerated domain with the edge as HTTPS origin. On the first domain of a root zone it proves ownership by writing the `verification` TXT record in Cloudflare. It installs the Traefik wildcard certificate (uploaded once per renewal and shared by every domain) and applies the baseline settings below. A failure here stops the deploy before anything has changed.
2. **Sync DNS** points the domain at the CDN CNAME. Switching between A and CNAME replaces the record in place.
3. The Nomad rollout and the public route probe run as before.
4. **Purge CDN** refreshes the `purge` paths after the new version is live. A refresh failure is a warning, never a failed deploy; run `luma cdn purge` to retry. A newly created domain is not refreshed because it has no cache yet.

Baseline settings Luma applies to every CDN domain:

- HTTPS to the origin, with SNI and the original `Host` header.
- HTTP requests are redirected to HTTPS.
- The application's cache headers decide caching. Responses without cache headers are not cached.
- Alibaba Cloud does not cache 404 and 5xx responses by default, so short errors during a rollout are not kept.

Control checks the Traefik certificate every hour and installs a renewed one on every CDN domain.

## Cache headers

The CDN saves traffic only for responses the application marks as cacheable. Before enabling it, check what the application returns:

```text
/_next/static/*, /assets/*.<hash>.js   Cache-Control: public, max-age=31536000, immutable
HTML pages                              Cache-Control: public, max-age=0, s-maxage=60
API, signed-in pages                    Cache-Control: private, no-store
```

Files with a content hash in the name never need a refresh: a new version has a new URL. Refresh only fixed URLs such as `/` or `/sitemap.xml`. A directory refresh (`/*`) counts against a quota of 100 per account per day, shared by all applications; URL refreshes allow 10,000 per day.

Give every personalized or signed-in response `Cache-Control: private, no-store`. Do not rely on the absence of headers: an `ETag` or `Last-Modified` header alone lets the CDN cache a response briefly.

Check a response through the CDN:

```bash
curl -sI https://www.example.com/ | grep -iE 'cache-control|x-cache|via'
```

## Operations

```bash
luma cdn status
luma cdn purge www.example.com / /pricing
luma cdn remove www.example.com
```

- `status` lists each domain with its CNAME, certificate expiry, last refresh and last error.
- `purge` refreshes paths now; without paths it refreshes `/`.
- `remove` deletes a CDN domain once DNS no longer points at it.

To turn the CDN off, remove `cdn` from the manifest and deploy. DNS points at the edge again and the Alibaba Cloud domain stays, so clients with a cached DNS answer keep working; delete it later with `luma cdn remove`. Removing the application deletes its CDN domain.

Traefik access logs keep the `Ali-Cdn-Real-Ip` header, which holds the real client address. Applications that need the client IP should read that header.
