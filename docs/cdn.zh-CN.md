# CDN {#cdn}

Luma 可以把服务放到阿里云 ESA（边缘安全加速）后面。manifest 里加一行即可开启，之后服务整个生命周期里的 ESA 记录和规则都由 Luma 管理。没写 `cdn` 的服务行为不变，也不会调用阿里云。

```text
User -> Alibaba Cloud ESA node -> (cache miss) -> CN Traefik -> service
```

## 什么时候用 {#when-to-use-it}

- 适用于 `cn-edge`、`external-edge`、`tailscale-relay` 的公网 HTTP 服务，其他暴露方式会被拒绝。
- 有 ICP 备案的域名用 `scope: global`，一个 CNAME 同时服务国内和海外用户；`scope: overseas` 不需要备案。
- DNS 继续放在 Cloudflare。Luma 写入指向 ESA 的仅 DNS CNAME；这些域名不要开启 Cloudflare 代理。

## 根域名 {#root-domains-apex}

CDN 放在 `www` 上，根域名跳转过去：

```yaml
name: web
image: ghcr.io/example/web:1.4.0
region: cn
exposure: cn-edge
domain: www.example.com
redirectFrom:
  hosts: [example.com]
  except: ["/api/*"]   # optional: served directly on example.com, never redirected
port: 3000
cdn: esa
```

- 根域名不能写成 CNAME，Cloudflare 只能"展平"：由 Cloudflare 自己去解析 ESA，再把 IP 返回给访客。这样 ESA 是按 Cloudflare 的位置给访客分配节点，而不是按访客自己的位置。itool.tech 上实测：国内访客被分到美国节点，一个页面要 10 秒。所以 Luma 拒绝在根域名上开 `cdn`，并在报错里给出下面的改法。
- `www.example.com` 是普通 CNAME，访客自己的 DNS 会直接去问 ESA，拿到就近的节点。
- `example.com` 保持直连 edge 的 A 记录。GET 和 HEAD 永久跳转到 `https://www.example.com`，保留路径和查询参数，浏览器会缓存这个跳转。其他方法直接转给应用，所以发到旧地址的支付回调和 webhook 仍然有效。
- `except` 列出在旧主机上对所有方法都直接提供、不跳转的路径。凡是其他客户端带 `Authorization` 头调用的 API 都应该列进来：浏览器和大多数 HTTP 库在跳转到另一个主机时会丢掉这个头，请求到达时就没有认证信息了。简写 `redirectFrom: [example.com]` 会让所有 GET 和 HEAD 都跳转。
- 应用的 canonical 地址、sitemap 和 OAuth 回调都要改成 `www`。WebAuthn 的 RP ID 保持根域名，passkey 就继续有效。

## 一次性准备 {#one-time-setup}

1. 在阿里云控制台购买 ESA 套餐。一个套餐对应一个根域名（一个 ESA 站点）。Luma 不会替你下单。
2. 创建一个只允许程序访问的 RAM 用户，授权以下策略（只包含 ESA 权限）：

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

3. 把这个 RAM 用户的 AccessKey 存入 Luma Control，并检查配置：

```bash
luma secret set LUMA_ALIBABA_CLOUD_ACCESS_KEY_ID
luma secret set LUMA_ALIBABA_CLOUD_ACCESS_KEY_SECRET
luma cdn check
```

变量名带 `LUMA_` 前缀，不会和应用自己使用的阿里云密钥冲突。不带域名的 `luma cdn check` 会检查密钥、ESA 套餐、Cloudflare zone 和 ESA 站点。

## 为服务开启 {#enable-it-for-a-service}

加上 `cdn: esa`，照常部署：

```yaml
name: web
image: ghcr.io/example/web:1.4.0
region: cn
exposure: cn-edge
domain: www.example.com
port: 3000
cdn: esa
```

完整字段：

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

`cache` 里只能写响应内容与登录状态无关的路径。应用标了 `private` 或 `no-store` 的响应仍然不会被缓存。

## 部署时发生什么 {#what-a-deploy-does}

1. **Prepare CDN**，在改 DNS 之前执行。某个 Luma zone 第一次接入时，Luma 以 CNAME 方式创建该 zone 的 ESA 站点，在 Cloudflare 写入 `_esaauth` TXT 记录完成归属验证，并写入下面的站点级配置。之后每个服务都会创建一条指向 edge 的代理加速记录，edge 地址变化时更新这条记录，并同步它的 `cache` 路径规则。接着写入 `_acme-challenge` CNAME，把证书验证委托给 ESA，并等待 ESA 签发免费证书，最多 15 分钟。这一步任何失败（包括证书还在签发中）都会在改 DNS 之前中止部署，稍后重新部署即可。ESA 会通过同一条委托记录自动续期证书。
2. **Sync DNS** 把域名指向 ESA 的 CNAME，此时 ESA 已经能提供 HTTPS。A 记录和 CNAME 之间切换时原地替换记录。
3. Nomad 发布照常进行。发布完成、跳转路由生效之后，`redirectFrom` 里的主机才切到 edge；这些主机的 DNS 失败只记警告。从 `redirectFrom` 删掉的主机，其 DNS 记录会被删除。
4. 公网路由探测直接连 edge，并以域名作为 SNI，这样 CDN 的报错不会触发对健康应用的恢复操作。
5. **Purge CDN** 在新版本上线后刷新缓存：默认刷新整个主机名，或只刷新 `purge` 里的路径。刷新失败只给警告，不会让部署失败，可以用 `luma cdn purge` 重试。新建的记录还没有缓存，不刷新。

站点级配置，由 ESA 站点下所有 Luma 域名共用：

- **回源**：始终用 HTTPS 回源，使用请求里的 `Host`，并校验 Traefik 的证书。HTTP 请求由 ESA 在边缘跳转到 HTTPS。
- **缓存**：用一条站点级规则让所有路径都可缓存，并遵循应用返回的缓存头，HTML 也一样；没有缓存头的响应不缓存。只靠 ESA 的全局缓存配置只能覆盖静态文件扩展名，HTML 仍然不会被缓存。
- Gzip、Brotli 和 Zstd 压缩，面向访客的 HTTP/2 和 HTTP/3，HTTP/2 回源，WebSocket，以及带真实客户端地址的 `ali-real-client-ip` 头。

每个 `cache` 路径为它的域名建一条规则：遵循应用的缓存头，没有缓存头时缓存 `ttl` 秒。路径规则在站点级规则之后执行，所以以它的 TTL 为准；`cache` 路径之间不能重叠。缓存规则按站点计配额，根域名下所有应用共用：Free 5 条、Basic 10 条、Standard 50 条。站点级规则占 1 条。超出时部署会在 Prepare CDN 这一步失败并说明原因。

其余由 ESA 默认提供：关闭 TLS 1.0、基础 DDoS 防护和托管 WAF 规则。智能路由等收费功能 Luma 不会开启。

## 尽可能多缓存 {#cache-as-much-as-possible}

没有自定义配置时，ESA 只缓存静态文件：HTML 和没有扩展名的路径无论应用返回什么都不缓存。Luma 的站点级缓存规则让应用的缓存头在所有路径上都生效，所以要多缓存，有两个办法：

- 让应用返回缓存头：

```text
/_next/static/*, /assets/*.<hash>.js   Cache-Control: public, max-age=31536000, immutable
HTML pages                              Cache-Control: public, max-age=0, s-maxage=60
API, signed-in pages                    Cache-Control: private, no-store
```

- 或者把对所有访客都一样的路径写进 `cdn.cache`，不用改应用。

先体检一下域名，看看现状。检查只读不写：匿名请求首页、`purge` 和 `cache` 里的路径、首页引用的几个静态资源、`/sitemap.xml` 和 `/robots.txt`，每个请求两次：

```bash
luma cdn check www.example.com
luma cdn check www.example.com --path /pricing --format json
```

- **DANGER**：可缓存的响应设置了 cookie，或者看起来像账号页面的路径标成了 public。必须先修，命令会以非零状态退出。
- **IMPROVE**：页面没有缓存头、页面每次都回源校验、静态文件没有缓存头，或者大文本没有压缩。
- **OK**：可缓存（第二次请求命中时说明已从边缘返回），或者明确标了不缓存。

文件名带内容 hash 的文件永远不需要刷新，新版本就是新地址。部署后的默认刷新覆盖整个主机名，只重新回源发生变化的资源。ESA 的主机名和目录刷新每个套餐每天约 100 次；URL 刷新的额度多得多。一天部署很多次的应用应在 `purge` 里列出固定地址。

## 日常运维 {#operations}

```bash
luma cdn status
luma cdn purge www.example.com
luma cdn purge www.example.com / /pricing
luma cdn remove www.example.com
```

- `status` 列出每个域名的站点、CNAME、最近一次刷新和最近的错误。
- `purge` 刷新整个主机名，或只刷新给出的路径。
- `remove` 在 DNS 不再指向 ESA 之后删除 ESA 记录和它的 `cache` 规则。

要为某个服务关闭 ESA，从 manifest 删掉 `cdn` 再部署。DNS 会改回指向 edge，ESA 记录保留，DNS 缓存还没过期的客户端仍能访问；之后再用 `luma cdn remove` 删除。删除应用时会一并删除它的 ESA 记录和规则，ESA 站点保留。
