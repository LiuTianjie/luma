# CDN {#cdn}

Luma 可以把服务放到阿里云 ESA（边缘安全加速）后面。manifest 里加一行即可开启，之后服务整个生命周期里的 ESA 记录都由 Luma 管理。没写 `cdn` 的服务行为不变，也不会调用阿里云。

```text
User -> Alibaba Cloud ESA node -> (cache miss) -> CN Traefik -> service
```

## 什么时候用 {#when-to-use-it}

- 适用于 `cn-edge`、`external-edge`、`tailscale-relay` 的公网 HTTP 服务，其他暴露方式会被拒绝。
- 有 ICP 备案的域名用 `scope: global`，一个 CNAME 同时服务国内和海外用户；`scope: overseas` 不需要备案。
- DNS 继续放在 Cloudflare。Luma 写入指向 ESA 的仅 DNS CNAME；这些域名不要开启 Cloudflare 代理。

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

变量名带 `LUMA_` 前缀，不会和应用自己使用的阿里云密钥冲突。`luma cdn check` 会检查密钥、ESA 套餐、Cloudflare zone 和 ESA 站点。

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
```

## 部署时发生什么 {#what-a-deploy-does}

1. **Prepare CDN**，在改 DNS 之前执行。某个 Luma zone 第一次接入时，Luma 以 CNAME 方式创建该 zone 的 ESA 站点，在 Cloudflare 写入 `_esaauth` TXT 记录完成归属验证，并写入下面的站点默认配置。之后每个服务都会创建一条指向 edge 的代理加速记录；edge 地址变化时会更新这条记录。这一步失败会中止部署，此时还没有任何改动。
2. **Sync DNS** 把域名指向 ESA 的 CNAME。A 记录和 CNAME 之间切换时原地替换记录。
3. **ESA certificate** 申请 ESA 的免费证书并等待签发，最多等 10 分钟。ESA 通过 HTTP 验证证书，所以只能在 DNS 指向 ESA 之后进行；首次切换的域名可能有几分钟没有 HTTPS 证书。证书由 ESA 自动续期。
4. Nomad 发布和公网路由探测照常进行。
5. **Purge CDN** 在新版本上线后刷新缓存：默认刷新整个主机名，或只刷新 `purge` 里的路径。刷新失败只给警告，不会让部署失败，可以用 `luma cdn purge` 重试。新建的记录还没有缓存，不刷新。

Luma 对每个 ESA 站点只写一次的默认配置。站点上已有同类全局配置时保持不变。

- 回源协议跟随客户端：HTTPS 请求用 HTTPS 回源，HTTP 请求由 Traefik 跳转到 HTTPS。
- Gzip 和 Brotli 压缩。
- 回源请求带 `ali-real-client-ip` 头，内容是真实客户端地址，Traefik 访问日志会保留它。

其余由 ESA 默认提供：HTTP/2 和 HTTP/3、关闭 TLS 1.0、基础 DDoS 防护和托管 WAF 规则。

## 缓存头 {#cache-headers}

只有应用标记为可缓存的响应，CDN 才能帮你省流量。开启前先检查应用返回的缓存头：

```text
/_next/static/*, /assets/*.<hash>.js   Cache-Control: public, max-age=31536000, immutable
HTML pages                              Cache-Control: public, max-age=0, s-maxage=60
API, signed-in pages                    Cache-Control: private, no-store
```

文件名带内容 hash 的文件永远不需要刷新，新版本就是新地址。所有个性化或登录后的响应都要返回 `Cache-Control: private, no-store`。

部署后的默认刷新覆盖整个主机名，只重新回源发生变化的资源。ESA 的主机名和目录刷新每个套餐每天约 100 次；URL 刷新的额度多得多。一天部署很多次的应用应在 `purge` 里列出固定地址。

通过 ESA 检查一个响应：

```bash
curl -sI https://www.example.com/ | grep -iE 'cache-control|x-site-cache-status|via'
```

## 日常运维 {#operations}

```bash
luma cdn status
luma cdn purge www.example.com
luma cdn purge www.example.com / /pricing
luma cdn remove www.example.com
```

- `status` 列出每个域名的站点、CNAME、最近一次刷新和最近的错误。
- `purge` 刷新整个主机名，或只刷新给出的路径。
- `remove` 在 DNS 不再指向 ESA 之后删除 ESA 记录。

要为某个服务关闭 ESA，从 manifest 删掉 `cdn` 再部署。DNS 会改回指向 edge，ESA 记录保留，DNS 缓存还没过期的客户端仍能访问；之后再用 `luma cdn remove` 删除。删除应用时会一并删除它的 ESA 记录，ESA 站点保留。
