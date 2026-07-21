import type { DashboardNode, DashboardStorageClass, Lang } from "../types";
import type { ComposeDeploymentDraft, ComposeServiceDraft, ComposeVolumeDraft, Exposure, KeyValueRow, Region } from "./types";
import { clearNodeIfIncompatible, EXPOSURES, exposureOptionLabel, hasReadyNodeInRegion, nodesForRegion, REGIONS, requiredRegionForExposure, regionOptionLabel } from "./options";
import { updateComposeServiceExposure } from "./yaml";

export function ComposeDeployForm({
  lang,
  draft,
  nodes,
  storageClasses,
  onChange,
  onEditYaml,
}: {
  lang: Lang;
  draft: ComposeDeploymentDraft;
  nodes: DashboardNode[];
  storageClasses: DashboardStorageClass[];
  onChange: (draft: ComposeDeploymentDraft) => void;
  onEditYaml: () => void;
}) {
  const zh = lang === "zh";
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
        <header><span>01</span><h3>{zh ? "应用配置" : "Application config"}</h3></header>
        <div className="deploy-field-grid">
          <label><span>{zh ? "应用名" : "Application name"}</span><input value={draft.name} onChange={(event) => patch({ name: event.target.value })} /></label>
          <label className="compose-file-field">
            <span>{zh ? "Compose 文件名" : "Compose file name"}</span>
            <input value={draft.composeFileName} onChange={(event) => patch({ composeFileName: event.target.value })} />
            <small>{zh ? "这里只改提交文件名，Compose 内容在 YAML 文件里编辑。" : "This only changes the submitted file name. Edit Compose content in the YAML view."}</small>
          </label>
          <div className="compose-yaml-shortcut">
            <div className="compose-yaml-shortcut-copy">
              <span>{zh ? "Compose 内容" : "Compose content"}</span>
              <small className="deploy-muted">{zh ? "在 YAML 视图中编辑完整 docker-compose.yml" : "Edit the full docker-compose.yml in the YAML view"}</small>
            </div>
            <button type="button" className="ghost" onClick={onEditYaml}>{zh ? "编辑 docker-compose.yml" : "Edit docker-compose.yml"}</button>
          </div>
          <label><span>{zh ? "默认区域" : "Default region"}</span><select value={draft.region} onChange={(event) => updateDefaultRegion(event.target.value as Region)}>{REGIONS.map((region) => <option key={region} value={region} disabled={nodes.length > 0 && !hasReadyNodeInRegion(nodes, region)}>{regionOptionLabel(nodes, region, lang)}</option>)}</select></label>
        </div>
      </section>
      <section className="deploy-config-section" id="compose-services">
        <header><span>02</span><h3>{zh ? "服务入口" : "Service ingress"}</h3></header>
        <div className="compose-service-list">
          {draft.services.map((service) => {
            const effectiveRegion = service.region || draft.region;
            const nodeOptions = nodesForRegion(nodes, effectiveRegion);
            const selectedNodeMissing = service.node && !nodeOptions.some((node) => node.name === service.node);
            return (
              <article className="compose-service-card" key={service.name}>
                <strong>{service.name}</strong>
              <div className="deploy-field-grid compact">
                <label><span>{zh ? "入口" : "Exposure"}</span><select value={service.exposure} onChange={(event) => updateServiceExposureSafe(service, event.target.value as Exposure)}>{EXPOSURES.map((exposure) => {
                  const requiredRegion = requiredRegionForExposure(exposure);
                  return <option key={exposure} value={exposure} disabled={Boolean(requiredRegion && nodes.length > 0 && !hasReadyNodeInRegion(nodes, requiredRegion))}>{exposureOptionLabel(nodes, exposure, lang)}</option>;
                })}</select></label>
                <label><span>{zh ? "区域" : "Region"}</span><select value={service.region} onChange={(event) => updateServiceRegion(service, event.target.value as Region | "")}><option value="">{zh ? `默认 (${draft.region})` : `Default (${draft.region})`}</option>{REGIONS.map((region) => <option key={region} value={region} disabled={nodes.length > 0 && !hasReadyNodeInRegion(nodes, region)}>{regionOptionLabel(nodes, region, lang)}</option>)}</select></label>
                <label>
                  <span>{zh ? "节点" : "Node"}</span>
                  <select value={service.node} onChange={(event) => updateService(service.name, { node: event.target.value })}>
                    <option value="">{zh ? `自动调度到 ${effectiveRegion} ready 节点` : `Auto-schedule to a ready ${effectiveRegion} node`}</option>
                    {selectedNodeMissing ? <option value={service.node} disabled>{service.node} ({zh ? "当前不可用" : "currently unavailable"})</option> : null}
                    {nodeOptions.map((node) => <option value={node.name || ""} key={node.name}>{node.name}</option>)}
                  </select>
                </label>
                <label><span>{zh ? "域名" : "Domain"}</span><input value={service.domain} disabled={service.exposure === "none"} onChange={(event) => updateService(service.name, { domain: event.target.value })} /></label>
                <label><span>{zh ? "容器端口" : "Container port"}</span><input value={service.port} disabled={service.exposure === "none"} onChange={(event) => updateService(service.name, { port: event.target.value })} /></label>
                <label><span>{zh ? "发布端口" : "Published port"}</span><input value={service.publishPort} disabled={!["tailscale-relay", "tcp-relay"].includes(service.exposure)} onChange={(event) => updateService(service.name, { publishPort: event.target.value })} /></label>
                <label><span>{zh ? "副本" : "Replicas"}</span><input type="number" min={1} value={service.replicas} onChange={(event) => updateService(service.name, { replicas: Number(event.target.value || 1) })} /></label>
                <label className="deploy-toggle"><input type="checkbox" checked={service.proxy} onChange={(event) => updateService(service.name, { proxy: event.target.checked })} /><span>egress proxy</span></label>
              </div>
              </article>
            );
          })}
        </div>
      </section>
      <section className="deploy-config-section" id="compose-env">
        <header><span>03</span><h3>{zh ? "环境变量与密钥" : "Environment and secrets"}</h3></header>
        <div className="compose-env-list">
          {draft.services.length ? draft.services.map((service) => (
            <article className="compose-service-card" key={`${service.name}-env`}>
              <div className="compose-env-header">
                <strong>{service.name}</strong>
                <div>
                  <button type="button" className="ghost" onClick={() => addEnv(service)}>{zh ? "添加变量" : "Add variable"}</button>
                  <button type="button" className="ghost" onClick={() => addEnv(service, "secret")}>{zh ? "添加密钥引用" : "Add secret reference"}</button>
                </div>
              </div>
              <p className="deploy-muted">{zh ? <>普通变量会写入 docker-compose.yml；密钥只填写 ${"{NAME}"} 引用，明文请先存入 Luma Control。</> : <>Plain variables are written to docker-compose.yml. Secrets must use ${"{NAME}"} references; store plaintext secrets in Luma Control first.</>}</p>
              {(service.env || []).length ? (service.env || []).map((row) => (
                <div className="deploy-env-row compose-env-row" key={row.id}>
                  <input value={row.key} onChange={(event) => updateEnv(service.name, row.id, { key: event.target.value })} placeholder="NAME" />
                  <select value={row.kind || "plain"} onChange={(event) => updateEnv(service.name, row.id, { kind: event.target.value as KeyValueRow["kind"] })}>
                    <option value="plain">{zh ? "普通变量" : "Plain variable"}</option>
                    <option value="secret">{zh ? "密钥引用" : "Secret reference"}</option>
                  </select>
                  <input
                    value={row.value}
                    onChange={(event) => updateEnv(service.name, row.id, { value: event.target.value })}
                    placeholder={row.kind === "secret" ? "${DATABASE_URL}" : "value"}
                  />
                  <button type="button" className="ghost" onClick={() => removeEnv(service, row.id)}>{zh ? "删除" : "Remove"}</button>
                </div>
              )) : <p className="deploy-muted">{zh ? "当前服务还没有环境变量。" : "This service has no environment variables yet."}</p>}
            </article>
          )) : <p className="deploy-muted">{zh ? "先在 docker-compose.yml 中声明服务，再配置服务环境变量。" : "Declare services in docker-compose.yml before configuring service environment variables."}</p>}
        </div>
      </section>
      <section className="deploy-config-section" id="compose-storage">
        <header><span>04</span><h3>{zh ? "存储卷" : "Volumes"}</h3></header>
        <div className="compose-volume-list">
          {draft.volumes.length ? draft.volumes.map((volume) => (
            <article className="compose-service-card" key={volume.name}>
              <strong>{volume.name}<small>{volume.target}</small></strong>
              <div className="deploy-field-grid compact">
                <label><span>{zh ? "存储模式" : "Storage mode"}</span><select value={volume.storageMode} onChange={(event) => updateVolume(volume.name, { storageMode: event.target.value as ComposeVolumeDraft["storageMode"] })}><option value="unmanaged">unmanaged volume</option><option value="storageClass">storageClass</option><option value="local">local node path</option></select></label>
                {volume.storageMode === "storageClass" ? (
                  <label><span>storageClass</span><select value={volume.storageClass} onChange={(event) => updateVolume(volume.name, { storageClass: event.target.value })}><option value="">{zh ? "选择已注册存储" : "Select registered storage"}</option>{storageClasses.map((item) => <option value={item.name || ""} key={item.name}>{item.name}</option>)}</select></label>
                ) : volume.storageMode === "local" ? (
                  <>
                    <label><span>{zh ? "节点" : "Node"}</span><select value={volume.localNode} onChange={(event) => updateVolume(volume.name, { localNode: event.target.value })}><option value="">{zh ? "选择 agent ready 节点" : "Select a ready agent node"}</option>{localReadyNodes.map((node) => <option value={node.name || ""} key={node.name}>{node.name}</option>)}</select></label>
                    <label><span>{zh ? "本地路径" : "Local path"}</span><input value={volume.localPath} onChange={(event) => updateVolume(volume.name, { localPath: event.target.value })} placeholder="/opt/luma/state/app" /></label>
                  </>
                ) : (
                  <label><span>{zh ? "说明" : "Note"}</span><input value={zh ? "保留为 Compose 命名卷，Luma 不接管存储后端" : "Kept as a Compose named volume; Luma does not manage the storage backend"} disabled /></label>
                )}
              </div>
            </article>
          )) : <p className="deploy-muted">{zh ? "当前 Compose 模板没有声明命名卷。" : "This Compose template does not declare named volumes."}</p>}
        </div>
      </section>
      <section className="deploy-config-section" id="compose-advanced">
        <header><span>05</span><h3>{zh ? "部署开关" : "Deploy options"}</h3></header>
        <div className="deploy-switch-grid">
          <label className="deploy-toggle">
            <input type="checkbox" checked={draft.skipDns} onChange={(event) => patch({ skipDns: event.target.checked })} />
            <div>
              <strong>{zh ? "跳过 DNS" : "Skip DNS"}</strong>
              <span>{zh ? "部署时不自动在 Cloudflare 上同步更新域名解析记录" : "Do not automatically sync Cloudflare DNS records during deploy."}</span>
            </div>
          </label>
          <label className="deploy-toggle">
            <input type="checkbox" checked={draft.skipOrchestrator} onChange={(event) => patch({ skipOrchestrator: event.target.checked })} />
            <div>
              <strong>{zh ? "跳过编排器" : "Skip orchestrator"}</strong>
              <span>{zh ? "只写入配置和路由，不提交 Nomad 部署" : "Write configuration and routes without submitting the Nomad deploy."}</span>
            </div>
          </label>
        </div>
      </section>
    </div>
  );
}
