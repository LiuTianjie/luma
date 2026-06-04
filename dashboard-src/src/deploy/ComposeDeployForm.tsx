import type { DashboardNode, DashboardStorageClass } from "../types";
import type { ComposeDeploymentDraft, ComposeServiceDraft, ComposeVolumeDraft, Exposure, KeyValueRow, Region } from "./types";
import { clearNodeIfIncompatible, EXPOSURES, exposureOptionLabel, hasReadyNodeInRegion, nodesForRegion, REGIONS, requiredRegionForExposure, regionOptionLabel } from "./options";
import { updateComposeServiceExposure } from "./yaml";

export function ComposeDeployForm({
  draft,
  nodes,
  storageClasses,
  onChange,
  onEditYaml,
}: {
  draft: ComposeDeploymentDraft;
  nodes: DashboardNode[];
  storageClasses: DashboardStorageClass[];
  onChange: (draft: ComposeDeploymentDraft) => void;
  onEditYaml: () => void;
}) {
  const localReadyNodes = nodes.filter((node) => node.agentStatus === "ready" && node.state !== "down");
  const patch = (next: Partial<ComposeDeploymentDraft>) => onChange({ ...draft, ...next });
  const updateService = (name: string, next: Partial<ComposeServiceDraft>) => {
    patch({ services: draft.services.map((service) => service.name === name ? { ...service, ...next } : service) });
  };
  const updateEnv = (serviceName: string, id: string, next: Partial<KeyValueRow>) => {
    const service = draft.services.find((item) => item.name === serviceName);
    if (!service) return;
    updateService(serviceName, { env: (service.env || []).map((row) => row.id === id ? { ...row, ...next } : row) });
  };
  const addEnv = (service: ComposeServiceDraft, kind: KeyValueRow["kind"] = "plain") => {
    updateService(service.name, { env: [...(service.env || []), { id: `env-${service.name}-${Date.now()}`, key: "", value: "", kind }] });
  };
  const updateDefaultRegion = (region: Region) => {
    patch({
      region,
      services: draft.services.map((service) => {
        if (service.region) return service;
        return { ...service, node: clearNodeIfIncompatible(nodes, service.node, region) };
      }),
    });
  };
  const updateServiceRegion = (service: ComposeServiceDraft, region: Region | "") => {
    const effectiveRegion = region || draft.region;
    updateService(service.name, { region, node: clearNodeIfIncompatible(nodes, service.node, effectiveRegion) });
  };
  const updateServiceExposureSafe = (service: ComposeServiceDraft, exposure: Exposure) => {
    const next = updateComposeServiceExposure(service, exposure);
    const effectiveRegion = next.region || draft.region;
    updateService(service.name, { ...next, node: clearNodeIfIncompatible(nodes, service.node, effectiveRegion) });
  };
  const removeEnv = (service: ComposeServiceDraft, id: string) => {
    updateService(service.name, { env: (service.env || []).filter((row) => row.id !== id) });
  };
  const updateVolume = (name: string, next: Partial<ComposeVolumeDraft>) => {
    patch({ volumes: draft.volumes.map((volume) => volume.name === name ? { ...volume, ...next } : volume) });
  };
  return (
    <div className="deploy-form-stack">
      <section className="deploy-config-section" id="compose-basic">
        <header><span>01</span><h3>应用配置</h3></header>
        <div className="deploy-field-grid">
          <label><span>应用名</span><input value={draft.name} onChange={(event) => patch({ name: event.target.value })} /></label>
          <label className="compose-file-field">
            <span>Compose 文件名</span>
            <input value={draft.composeFileName} onChange={(event) => patch({ composeFileName: event.target.value })} />
            <small>这里只改提交文件名，Compose 内容在 YAML 文件里编辑。</small>
          </label>
          <div className="compose-yaml-shortcut">
            <span>Compose 内容</span>
            <button type="button" className="ghost" onClick={onEditYaml}>编辑 docker-compose.yml</button>
          </div>
          <label><span>默认区域</span><select value={draft.region} onChange={(event) => updateDefaultRegion(event.target.value as Region)}>{REGIONS.map((region) => <option key={region} value={region} disabled={nodes.length > 0 && !hasReadyNodeInRegion(nodes, region)}>{regionOptionLabel(nodes, region)}</option>)}</select></label>
        </div>
      </section>
      <section className="deploy-config-section" id="compose-services">
        <header><span>02</span><h3>服务入口</h3></header>
        <div className="compose-service-list">
          {draft.services.map((service) => {
            const effectiveRegion = service.region || draft.region;
            const nodeOptions = nodesForRegion(nodes, effectiveRegion);
            const selectedNodeMissing = service.node && !nodeOptions.some((node) => node.name === service.node);
            return (
              <article className="compose-service-card" key={service.name}>
                <strong>{service.name}</strong>
              <div className="deploy-field-grid compact">
                <label><span>入口</span><select value={service.exposure} onChange={(event) => updateServiceExposureSafe(service, event.target.value as Exposure)}>{EXPOSURES.map((exposure) => {
                  const requiredRegion = requiredRegionForExposure(exposure);
                  return <option key={exposure} value={exposure} disabled={Boolean(requiredRegion && nodes.length > 0 && !hasReadyNodeInRegion(nodes, requiredRegion))}>{exposureOptionLabel(nodes, exposure)}</option>;
                })}</select></label>
                <label><span>区域</span><select value={service.region} onChange={(event) => updateServiceRegion(service, event.target.value as Region | "")}><option value="">默认 ({draft.region})</option>{REGIONS.map((region) => <option key={region} value={region} disabled={nodes.length > 0 && !hasReadyNodeInRegion(nodes, region)}>{regionOptionLabel(nodes, region)}</option>)}</select></label>
                <label>
                  <span>节点</span>
                  <select value={service.node} onChange={(event) => updateService(service.name, { node: event.target.value })}>
                    <option value="">自动调度到 {effectiveRegion} ready 节点</option>
                    {selectedNodeMissing ? <option value={service.node} disabled>{service.node} (当前不可用)</option> : null}
                    {nodeOptions.map((node) => <option value={node.name || ""} key={node.name}>{node.name}</option>)}
                  </select>
                </label>
                <label><span>域名</span><input value={service.domain} disabled={service.exposure === "none"} onChange={(event) => updateService(service.name, { domain: event.target.value })} /></label>
                <label><span>容器端口</span><input value={service.port} disabled={service.exposure === "none"} onChange={(event) => updateService(service.name, { port: event.target.value })} /></label>
                <label><span>发布端口</span><input value={service.publishPort} disabled={service.exposure !== "tailscale-relay"} onChange={(event) => updateService(service.name, { publishPort: event.target.value })} /></label>
                <label><span>副本</span><input type="number" min={1} value={service.replicas} onChange={(event) => updateService(service.name, { replicas: Number(event.target.value || 1) })} /></label>
                <label className="deploy-toggle"><input type="checkbox" checked={service.proxy} onChange={(event) => updateService(service.name, { proxy: event.target.checked })} /><span>egress proxy</span></label>
              </div>
              </article>
            );
          })}
        </div>
      </section>
      <section className="deploy-config-section" id="compose-env">
        <header><span>03</span><h3>环境变量与密钥</h3></header>
        <div className="compose-env-list">
          {draft.services.length ? draft.services.map((service) => (
            <article className="compose-service-card" key={`${service.name}-env`}>
              <div className="compose-env-header">
                <strong>{service.name}</strong>
                <div>
                  <button type="button" className="ghost" onClick={() => addEnv(service)}>添加变量</button>
                  <button type="button" className="ghost" onClick={() => addEnv(service, "secret")}>添加密钥引用</button>
                </div>
              </div>
              <p className="deploy-muted">普通变量会写入 docker-compose.yml；密钥只填写 ${"{NAME}"} 引用，明文请先存入 Luma Control。</p>
              {(service.env || []).length ? (service.env || []).map((row) => (
                <div className="deploy-env-row compose-env-row" key={row.id}>
                  <input value={row.key} onChange={(event) => updateEnv(service.name, row.id, { key: event.target.value })} placeholder="NAME" />
                  <select value={row.kind || "plain"} onChange={(event) => updateEnv(service.name, row.id, { kind: event.target.value as KeyValueRow["kind"] })}>
                    <option value="plain">普通变量</option>
                    <option value="secret">密钥引用</option>
                  </select>
                  <input
                    value={row.value}
                    onChange={(event) => updateEnv(service.name, row.id, { value: event.target.value })}
                    placeholder={row.kind === "secret" ? "${DATABASE_URL}" : "value"}
                  />
                  <button type="button" className="ghost" onClick={() => removeEnv(service, row.id)}>删除</button>
                </div>
              )) : <p className="deploy-muted">当前服务还没有环境变量。</p>}
            </article>
          )) : <p className="deploy-muted">先在 docker-compose.yml 中声明服务，再配置服务环境变量。</p>}
        </div>
      </section>
      <section className="deploy-config-section" id="compose-storage">
        <header><span>04</span><h3>存储卷</h3></header>
        <div className="compose-volume-list">
          {draft.volumes.length ? draft.volumes.map((volume) => (
            <article className="compose-service-card" key={volume.name}>
              <strong>{volume.name}<small>{volume.target}</small></strong>
              <div className="deploy-field-grid compact">
                <label><span>存储模式</span><select value={volume.storageMode} onChange={(event) => updateVolume(volume.name, { storageMode: event.target.value as ComposeVolumeDraft["storageMode"] })}><option value="unmanaged">unmanaged volume</option><option value="storageClass">storageClass</option><option value="local">local node path</option></select></label>
                {volume.storageMode === "storageClass" ? (
                  <label><span>storageClass</span><select value={volume.storageClass} onChange={(event) => updateVolume(volume.name, { storageClass: event.target.value })}><option value="">选择已注册存储</option>{storageClasses.map((item) => <option value={item.name || ""} key={item.name}>{item.name}</option>)}</select></label>
                ) : volume.storageMode === "local" ? (
                  <>
                    <label><span>节点</span><select value={volume.localNode} onChange={(event) => updateVolume(volume.name, { localNode: event.target.value })}><option value="">选择 agent ready 节点</option>{localReadyNodes.map((node) => <option value={node.name || ""} key={node.name}>{node.name}</option>)}</select></label>
                    <label><span>本地路径</span><input value={volume.localPath} onChange={(event) => updateVolume(volume.name, { localPath: event.target.value })} placeholder="/opt/luma/state/app" /></label>
                  </>
                ) : (
                  <label><span>说明</span><input value="保留为 Compose 命名卷，Luma 不接管存储后端" disabled /></label>
                )}
              </div>
            </article>
          )) : <p className="deploy-muted">当前 Compose 模板没有声明命名卷。</p>}
        </div>
      </section>
      <section className="deploy-config-section" id="compose-advanced">
        <header><span>05</span><h3>部署开关</h3></header>
        <div className="deploy-switch-grid">
          <label className="deploy-toggle">
            <input type="checkbox" checked={draft.skipDns} onChange={(event) => patch({ skipDns: event.target.checked })} />
            <div>
              <strong>跳过 DNS</strong>
              <span>部署时不自动在 Cloudflare 上同步更新域名解析记录</span>
            </div>
          </label>
          <label className="deploy-toggle">
            <input type="checkbox" checked={draft.skipWebhook} onChange={(event) => patch({ skipWebhook: event.target.checked })} />
            <div>
              <strong>跳过 Portainer</strong>
              <span>部署时不触发 Portainer Webhook 触发 Swarm 容器重启更新</span>
            </div>
          </label>
        </div>
      </section>
    </div>
  );
}
