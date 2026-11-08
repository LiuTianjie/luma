# CDN {#cdn}

Luma 可以把服务放到阿里云 CDN 后面。manifest 里加一行即可开启，之后在服务的整个生命周期里，CDN 域名都由 Luma 管理。

```text
User -> Alibaba Cloud CDN node -> (cache miss) -> CN Traefik -> service
```

## 什么时候用 CDN {#when-to-use-the-cdn}

- 适用于 `cn-edge`、`external-edge`、`tailscale-relay` 的公网 HTTP 服务，其他暴露方式会被拒绝。
- 有 ICP 备案的域名用 `scope: global`，一个 CNAME 同时服务国内和海外用户。
- 没有备案或只服务海外的域名用 `scope: overseas`，不需要备案。
- 这些域名不要开启 Cloudflare 代理（橙云）。DNS 继续放在 Cloudflare，记录是仅 DNS 的 CNAME。

## 一次性准备 {#one-time-setup}

1. 在阿里云控制台开通 CDN 服务。不要在控制台添加域名，域名由 Luma 添加。
2. 创建一个只允许程序访问的 RAM 用户，授权以下自定义策略：

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

3. 把这个 RAM 用户的 AccessKey 存入 Luma Control，并检查配置：

```bash
luma secret set ALIBABA_CLOUD_ACCESS_KEY_ID
luma secret set ALIBABA_CLOUD_ACCESS_KEY_SECRET
luma cdn check
```

`luma cdn check` 会检查密钥、CDN 与证书 API、Traefik 证书库和 Cloudflare zone。证书库检查要求 manager 已经更新到包含这个功能的 Luma 版本（`luma update`），因为 Control 需要以只读方式读取 Traefik 的证书卷。

## 为服务开启 CDN {#enable-the-cdn-for-a-service}

加上 `cdn: aliyun`，照常部署：

```yaml
name: web
image: ghcr.io/example/web:1.4.0
region: cn
exposure: cn-edge
domain: www.example.com
port: 3000
cdn: aliyun
```

完整字段：

```yaml
cdn:
  provider: aliyun              # only aliyun today
  scope: global                 # global (default) | domestic | overseas
  purge: ["/", "/sitemap.xml"]  # refreshed after each deploy; default ["/"]; "/blog/*" refreshes a directory
```

第一次带 `cdn` 部署时，阿里云需要 3 到 10 分钟让域名上线。之后的部署只多几秒。

## 部署时发生什么 {#what-a-deploy-does}

1. **Prepare CDN**，在改 DNS 之前执行。Luma 创建加速域名，回源到 edge 的 HTTPS。某个根域名第一次接入时，Luma 在 Cloudflare 写入 `verification` TXT 记录完成归属验证。随后安装 Traefik 的泛域名证书（每次续期只上传一次，各域名共用），并写入下面的基础配置。这一步失败会中止部署，此时还没有任何改动。
2. **Sync DNS** 把域名指向 CDN 的 CNAME。A 记录和 CNAME 之间切换时原地替换记录。
3. Nomad 发布和公网路由探测照常进行。
4. **Purge CDN** 在新版本上线后刷新 `purge` 里的路径。刷新失败只给警告，不会让部署失败，可以用 `luma cdn purge` 重试。新建的域名还没有缓存，不刷新。

Luma 给每个 CDN 域名写入的基础配置：

- 用 HTTPS 回源，带 SNI 和原始 `Host` 头。
- HTTP 请求跳转到 HTTPS。
- 是否缓存由应用的缓存头决定。没有缓存头的响应不缓存。
- 阿里云默认不缓存 404 和 5xx 响应，发布过程中短暂的错误不会被缓存下来。

Control 每小时检查一次 Traefik 证书，续期后自动安装到所有 CDN 域名。

## 缓存头 {#cache-headers}

只有应用标记为可缓存的响应，CDN 才能帮你省流量。开启前先检查应用返回的缓存头：

```text
/_next/static/*, /assets/*.<hash>.js   Cache-Control: public, max-age=31536000, immutable
HTML pages                              Cache-Control: public, max-age=0, s-maxage=60
API, signed-in pages                    Cache-Control: private, no-store
```

文件名带内容 hash 的文件永远不需要刷新，新版本就是新地址。只需要刷新 `/`、`/sitemap.xml` 这类固定地址。目录刷新（`/*`）每个账号每天只有 100 次，所有应用共用；URL 刷新每天有 10,000 次。

所有个性化或登录后的响应都要返回 `Cache-Control: private, no-store`。不要依赖“没写缓存头”：只要带了 `ETag` 或 `Last-Modified`，CDN 就可能短暂缓存这个响应。

通过 CDN 检查一个响应：

```bash
curl -sI https://www.example.com/ | grep -iE 'cache-control|x-cache|via'
```

## 日常运维 {#operations}

```bash
luma cdn status
luma cdn purge www.example.com / /pricing
luma cdn remove www.example.com
```

- `status` 列出每个域名的 CNAME、证书到期时间、最近一次刷新和最近的错误。
- `purge` 立即刷新路径；不写路径时刷新 `/`。
- `remove` 在 DNS 不再指向 CDN 之后删除 CDN 域名。

要关闭 CDN，从 manifest 删掉 `cdn` 再部署。DNS 会改回指向 edge，阿里云上的域名保留，DNS 缓存还没过期的客户端仍能访问；之后再用 `luma cdn remove` 删除。删除应用时会一并删除它的 CDN 域名。

Traefik 访问日志会保留 `Ali-Cdn-Real-Ip` 头，里面是真实的客户端地址。需要客户端 IP 的应用应读取这个头。
