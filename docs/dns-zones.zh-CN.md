# DNS 域名 {#dns-zones}

Luma 为每个公网服务写 DNS 解析记录。记录写在拥有该域名的 zone 里，所以一个集群可以同时使用托管在不同服务商的域名。

| 服务商 | `--provider` | 凭据 |
|---|---|---|
| Cloudflare | `cloudflare` | 具有 Zone → DNS → Edit 权限的 API Token |
| 阿里云 DNS | `alidns` | RAM 子账号的 AccessKey ID 和 Secret，授予 AliyunDNSFullAccess |
| 腾讯云 DNSPod | `dnspod` | CAM 子用户的 SecretId 和 SecretKey，授予 QcloudDNSPodFullAccess |

## 主域名 {#the-primary-zone}

首次安装时配置的 Cloudflare zone（`luma manager cloudflare --zone`）是主域名，同时用于 Control 域名、ESA 验证记录和 Cloudflare Tunnel。不属于任何已添加 zone 的域名继续使用主域名，已有集群的行为不变。

## 添加域名 {#add-a-zone}

```bash
luma dns zone add example.cn --provider alidns
```

CLI 逐项提示输入凭据，不回显。在 CI 中改为读取各厂商自己工具使用的环境变量：

- Cloudflare：`CLOUDFLARE_API_TOKEN`；
- 阿里云：`ALIBABA_CLOUD_ACCESS_KEY_ID`、`ALIBABA_CLOUD_ACCESS_KEY_SECRET`；
- 腾讯云：`TENCENTCLOUD_SECRET_ID`、`TENCENTCLOUD_SECRET_KEY`。

保存前 Control 会调用一次服务商接口。读不到该 zone 的密钥会被拒绝，什么都不保存。保存的密钥是名为 `LUMA_DNS_<ZONE>_<FIELD>` 的 Control 密钥，API 不会返回它们。

同一账号下的第二个域名可以共用密钥：

```bash
luma dns zone add example.com.cn --provider alidns --same-as example.cn
```

控制台在「基础设施 → 域名」提供同样的操作。

## 使用域名 {#use-a-zone}

清单不需要改。`domain: api.example.cn` 的服务在下次部署时把记录写进 `example.cn`。两个 zone 都匹配时取更长的：同时存在 `example.cn` 和 `shop.example.cn` 时，`api.shop.example.cn` 使用后者。

```yaml
name: api
image: ghcr.io/your-org/api:latest
region: cn
exposure: cn-edge
domain: api.example.cn
port: 8080
```

非 Cloudflare 服务商的差异：

- `dns.proxied` 是 Cloudflare 功能，其他服务商会直接报错；
- 两家国内服务商的免费版要求 TTL 至少 600 秒，更低的值会被提高到 600；
- 记录写在默认线路；
- [CDN](cdn.zh-CN.md) 目前只支持主域名下的域名。

## 证书 {#certificates}

Manager 上的 Traefik 继续用 Cloudflare DNS-01 验证主域名，并保留通配证书。阿里云或 DNSPod zone 里的域名使用第二个签发方式，通过 80 端口做 HTTP-01 验证，因此签发前 DNS 记录必须已经指向入口。Luma 在发布之前写 DNS，所以部署后的第一次访问可能要等几秒签发证书。

[`node-edge`](exposure-model.zh-CN.md#node-edge) 服务不论域名属于哪个 zone，都使用 HTTP-01。

## 检查、轮换与移除 {#check-rotate-and-remove}

```bash
luma dns zone list
luma dns zone check example.cn
luma dns zone add example.cn --provider alidns --update
luma dns zone remove example.cn
```

`check` 只从服务商读取。`--update` 替换已保存的密钥，留空的字段保持原值。仍有部署的域名或 `redirectFrom` 在该 zone 下时，`remove` 会被拒绝；移除不会删除服务商那边已有的记录。
