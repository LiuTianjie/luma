# Service Patterns

本仓库固定四种服务模式。新增服务时优先复制 `templates/` 中的模板，再修改镜像、域名、端口、region 和 replicas。

## 1. public-cn-service

国内公开服务。

- 有公网域名。
- 接入国内 Traefik。
- 跑在 `region=cn`。
- 适合主 Web/API 和核心业务服务。

典型约束：

```yaml
placement:
  constraints:
    - node.labels.region == cn
```

公开域名通过 Traefik labels 声明。

## 2. public-global-service

国内域名入口，容器跑在海外节点。

- 有公网域名。
- 用户仍访问备案域名。
- 国内 Traefik 作为入口。
- 服务容器跑在 `region=global`。
- 适合 AI 网关、外网代理、低频外网服务。
- 不适合核心高频 API。

这个模式会引入跨 region 请求链路。只有当服务必须访问外网，且请求频率或延迟要求可接受时才使用。

## 3. global-worker

海外 worker。

- 无公网域名。
- 跑在 `region=global`。
- 通常同时要求 `external_net=true`。
- 通过队列消费任务。
- 访问外网后写回结果。

推荐链路：

```text
cn Web/API -> Queue -> global worker -> 外网 API -> 写回结果
```

这种模式比国内 API 实时调用海外 HTTP 更稳，也更容易重试、限流和降级。

## 4. home-internal-service

家庭节点内部服务。

- 跑在 `region=home`。
- 非核心。
- 默认不暴露公网域名。
- 优先通过 Tailscale 或内网访问。

适合备份、低频任务、内部工具和测试服务。不要把核心公网服务调度到 home。
