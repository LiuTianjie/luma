# Operations

常见运维动作可以通过 Portainer 完成，也可以在 Swarm manager 节点上用 Docker CLI 执行。生产变更优先走 Git，保持本仓库是部署事实来源。

## 新增服务

1. 从 `examples/` 复制一个 service manifest。
2. 修改 `image`、域名、端口、region、exposure、replicas 和环境变量。
3. 本地预览：`luma deploy <service>.yaml --dry-run`。
4. 本地生成但不触发外部系统：`luma deploy <service>.yaml --skip-dns --skip-webhook`。
5. 真实发布：`luma deploy <service>.yaml --commit --push`。
6. 运行 `./scripts/validate-stacks.sh`。

需要完全手写 stack 时仍可复制 `templates/`，但默认使用 Luma。

## 更新镜像 tag

优先修改对应 service manifest 的 `image` tag。

```yaml
image: ghcr.io/your-org/your-app:2026-05-29-1
```

然后执行：

```bash
luma deploy <service>.yaml --commit --push
```

如果是手写 stack，提交后在 Portainer 更新 stack，或执行：

```bash
docker stack deploy -c stacks/cn/your-app/stack.yml your-app
```

## 扩缩容 replicas

优先修改 service manifest 的 `replicas` 后重新执行 Luma。

```yaml
replicas: 3
```

```bash
luma deploy <service>.yaml --commit --push
```

临时扩缩容也可以执行：

```bash
docker service scale <stack>_<service>=3
```

临时命令不会写回 Git，最终仍应更新本仓库。

## 查看日志

```bash
docker service logs -f <stack>_<service>
```

查看最近日志：

```bash
docker service logs --tail 200 <stack>_<service>
```

## 回滚 stack

优先用 Git 回滚 `stack.yml` 到上一个可用版本，然后重新部署。

```bash
git revert <commit>
docker stack deploy -c <stack-file> <stack-name>
```

如果只是单个 service 的镜像更新失败，也可以尝试：

```bash
docker service rollback <stack>_<service>
```

## 下线服务

从 Portainer 删除 stack，或执行：

```bash
docker stack rm <stack-name>
```

然后从仓库删除对应 `stacks/<region>/<service>/` 目录并提交。

如果服务使用 `tailscale-relay`，还要删除对应 `routes/<service>.yml` 并同步 `/opt/luma/routes`。

## 节点临时摘除

维护节点前先 drain，避免新任务调度到该节点。

```bash
docker node update --availability drain <node-name>
```

维护完成后恢复：

```bash
docker node update --availability active <node-name>
```

## 检查服务实际运行在哪个节点

```bash
docker service ps <stack>_<service>
```

查看完整任务和节点信息：

```bash
docker service ps --no-trunc <stack>_<service>
```

## Portainer 安全

Portainer 管理面板不要直接暴露公网。推荐只通过 Tailscale IP、内网地址或受控 VPN 访问。若未来确实需要公网访问，必须额外加认证、访问控制和审计。

## Tailscale relay 安全

`tailscale-relay` 会把公网请求从国内 Traefik 转发到 home 节点。使用时要满足：

- home 服务端口只允许 Tailscale 网络访问；
- 不用于核心高频业务；
- 不用于大文件下载；
- `routes/<service>.yml` 已同步到国内入口节点 `/opt/luma/routes`；
- 国内入口节点能解析并访问 `relay.host`。
