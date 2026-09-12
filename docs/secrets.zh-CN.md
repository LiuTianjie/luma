# 密钥与凭据 {#secrets}

Luma 将项目拓扑存放在 `luma.yaml`，密钥则保留在 Git 之外。

CLI 默认加载当前工作目录的 `.env` 和当前用户的 `~/.luma.config.json`。可用 `--env-file <path>` 指定其他项目本地文件，或用 `--no-env` 禁止加载本地密钥。

正常使用时直接运行目标命令即可。本地缺少配置值时，Luma 会先提示输入，再继续执行：

```bash
luma bootstrap manager --domain luma.example.com
luma node join https://luma.example.com --token <node-join-token> --region global --name global-sg-1
```

这会以 `0600` 权限写入 `~/.luma.config.json`。仍可通过 `luma configure --role manager|worker` 预填配置；`luma configure --show` 会遮蔽密钥值。

`.env` 仍适合开发或一次性的配置覆盖：

```bash
cp .env.example .env
$EDITOR .env
```

## 环境变量 {#environment-variables}

| 变量 | 使用时机 | 用途 |
| --- | --- | --- |
| `CLOUDFLARE_API_TOKEN` | Manager 必需 | 具有 Zone Read 和 DNS Edit 权限的 Cloudflare DNS API 令牌，用于创建或更新 Control 与服务的 DNS 记录。 |
| `LUMA_DNS_EDGE_TARGET` | Manager 通常需要 | `luma.yaml` 尚未配置边缘目标时，Cloudflare 记录应指向的公网 IP 或 DNS 名称。 |
| `TRAEFIK_ACME_EMAIL` | Manager 必需 | Traefik 申请 HTTPS 证书和接收过期通知所用的 Let's Encrypt 账户邮箱。 |
| `EGRESS_SUBSCRIPTION_URL` | 使用出网代理时必需 | 用于生成 Mihomo 配置的代理订阅 URL，供镜像拉取和 `proxy: true` 的服务使用。 |
| `TAILSCALE_AUTHKEY` | 私网/家庭/tailscale-relay 节点需要 | 无人值守登录 Tailscale 的认证密钥。 |
| `LUMA_SUDO_PASSWORD` | sudo 要求密码时需要 | 本地特权安装命令使用的备用密码，条件允许时优先使用免密 sudo。 |
| `LUMA_CONTROL_IMAGE` | 开发或固定版本时使用 | Manager 初始化使用的 Control API 镜像。 |

## 运行时密钥文件 {#runtime-secret-files}

控制面状态写在 Manager 上：

```text
/opt/luma/control/control.sqlite3
```

其中包含集群 ID、管理令牌、节点加入令牌、Nomad gossip key，以及 Control API 所需的 Cloudflare 环境变量副本。数据库、运行时 WAL/SHM 文件和备份都不能提交到 Git 或复制到客户端。旧版 `control.json` 仅作为初次迁移输入，迁移后不要再编辑。关于一致性备份和外部密钥文件，见[控制面存储与恢复](control-storage.md)。

Luma Control 还挂载 `/var/run/docker.sock` 并访问 Nomad HTTP API，以便客户端加入后记录节点元数据。管理令牌和节点加入令牌都应按集群管理级敏感凭据保管。

## 部署密钥 {#deployment-secrets}

服务清单可在 `env` 等字段中通过 `${NAME}` 引用控制面的部署密钥。

多数项目部署可以直接传入已有的 `.env` 文件：

```bash
luma deploy service.yaml --env .env
luma compose deploy luma.compose.yml --env .env
```

Luma 按服务或 Compose 的 `name`，将这些值保存在所部署应用的作用域内。名为 `api` 和 `worker` 的服务都可以使用 `DATABASE_URL`，互不覆盖。只会导入清单或 Compose 内容实际引用的变量。

也可以手动管理应用作用域密钥：

```bash
luma secret set DATABASE_URL --scope api
luma secret import .env --scope api
```

应用没有作用域密钥时，旧版全局部署密钥仍然有效：

```bash
luma login https://luma.example.com --token <management-token>
luma secret set API_TOKEN
luma secret list
```

`luma secret list` 只输出名称，不显示值。部署时，Luma Control 在 Manager 上解析引用值，再渲染 Nomad job。清单引用了不存在的密钥时，部署会在提交 job 之前失败：

```yaml
env:
  API_TOKEN: ${API_TOKEN}
```

出网配置写在节点上：

```text
/opt/luma/egress-gateway/config.yaml
```

此文件不能提交到 Git。

## 镜像仓库凭据 {#registry-credentials}

私有镜像仓库凭据与部署密钥分开保存：

```bash
luma login https://luma.example.com --token <management-token>
printf '%s' "$GHCR_TOKEN" | luma registry login ghcr.io --username <user> --password-stdin
luma registry list
```

配置 Builder Registry 后，Luma 只把源仓库凭据短期授权给 Builder 镜像缓存任务。它们不会持久化到 agent 任务、渲染到清单 YAML、注入运行时 job 或传给服务容器。如果内部 Builder Registry 本身需要认证，可将它独立的凭据注入 Nomad docker 的 `config.auth`。`luma registry list` 仅返回仓库主机名和用户名。

`luma registry remove <host>` 从 Luma Control 删除凭据，但不会撤销提供方签发的令牌，也不能清除已经写入运行中 job 规格的认证信息。需要使访问失效时，应在仓库提供方轮换或撤销令牌。

Git 忽略 `.env` 和 `.env.*`，仅提交安全模板 `.env.example`。

客户端登录状态按用户保存：

```text
~/.luma.config.json
~/.config/luma/contexts/<cluster>.json
~/.config/luma/current-context
```

`~/.luma.config.json` 包含 Cloudflare、Tailscale、出网和 sudo 等本地安装密钥。Context 仅包含 Control 端点、集群 ID 和管理令牌。两者都应作为密钥保管。

`https://<control-domain>/dashboard/` 的网页控制台也使用管理令牌。浏览器会在该 Control 域名的本地存储中保存令牌，因此只应在可信设备上使用。设备不再可信时，请清理浏览器存储或轮换管理令牌。

## 轮换 {#rotation}

令牌或订阅 URL 出现在以下位置时，应立即轮换：

- 聊天记录；
- Shell 历史；
- 日志；
- 截图；
- Git 提交。