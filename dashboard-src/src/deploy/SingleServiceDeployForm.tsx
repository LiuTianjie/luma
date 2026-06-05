import type { DashboardNode, DashboardStorageClass } from "../types";
import type { Exposure, KeyValueRow, Region, ServiceManifestDraft, ServiceVolumeDraft } from "./types";
import { clearNodeIfIncompatible, EXPOSURES, exposureOptionLabel, hasReadyNodeInRegion, nodesForRegion, REGIONS, requiredRegionForExposure, regionOptionLabel } from "./options";
import { serviceExposureRegion } from "./yaml";

export function SingleServiceDeployForm({
  draft,
  nodes,
  storageClasses,
  onChange,
}: {
  draft: ServiceManifestDraft;
  nodes: DashboardNode[];
  storageClasses: DashboardStorageClass[];
  onChange: (draft: ServiceManifestDraft) => void;
}) {
  const patch = (next: Partial<ServiceManifestDraft>) => onChange({ ...draft, ...next });
  const volumeMounts = draft.volumeMounts || [];
  const nodeOptions = nodesForRegion(nodes, draft.region);
  const selectedNodeMissing = draft.node && !nodeOptions.some((node) => node.name === draft.node);
  const patchRegion = (region: Region) => {
    patch({ region, node: clearNodeIfIncompatible(nodes, draft.node, region) });
  };
  const patchExposure = (exposure: Exposure) => {
    const region = serviceExposureRegion(exposure, draft.region);
    patch({ exposure, region, node: clearNodeIfIncompatible(nodes, draft.node, region) });
  };
  const updateEnv = (id: string, next: Partial<KeyValueRow>) => {
    patch({ env: draft.env.map((row) => row.id === id ? { ...row, ...next } : row) });
  };
  const updateVolumeMount = (id: string, next: Partial<ServiceVolumeDraft>) => {
    patch({ volumeMounts: volumeMounts.map((volume) => volume.id === id ? { ...volume, ...next } : volume) });
  };
  const addVolumeMount = () => {
    patch({
      volumeMounts: [
        ...volumeMounts,
        {
          id: `service-volume-${Date.now()}`,
          name: "",
          target: "",
          storageMode: "unmanaged",
          storageClass: "",
          path: "",
        },
      ],
    });
  };
  return (
    <div className="deploy-form-stack">
      <section className="deploy-config-section" id="deploy-basic">
        <header><span>01</span><h3>基础配置</h3></header>
        <div className="deploy-field-grid">
          <label><span>服务名</span><input value={draft.name} onChange={(event) => patch({ name: event.target.value })} /></label>
          <label><span>镜像</span><input value={draft.image} onChange={(event) => patch({ image: event.target.value })} /></label>
          <label><span>区域</span><select value={draft.region} onChange={(event) => patchRegion(event.target.value as Region)}>{REGIONS.map((region) => <option key={region} value={region} disabled={nodes.length > 0 && !hasReadyNodeInRegion(nodes, region)}>{regionOptionLabel(nodes, region)}</option>)}</select></label>
          <label>
            <span>节点</span>
            <select value={draft.node} onChange={(event) => patch({ node: event.target.value })}>
              <option value="">自动调度到 {draft.region} ready 节点</option>
              {selectedNodeMissing ? <option value={draft.node} disabled>{draft.node} (当前不可用)</option> : null}
              {nodeOptions.map((node) => <option value={node.name || ""} key={node.name}>{node.name}</option>)}
            </select>
            <small>仅在必须固定机器时选择节点。</small>
          </label>
          <label><span>副本</span><input type="number" min={1} value={draft.replicas} onChange={(event) => patch({ replicas: Number(event.target.value || 1) })} /></label>
          <label className="deploy-toggle"><input type="checkbox" checked={draft.proxy} onChange={(event) => patch({ proxy: event.target.checked })} /><span>启用 egress proxy</span></label>
        </div>
      </section>
      <section className="deploy-config-section" id="deploy-network">
        <header><span>02</span><h3>入口与网络</h3></header>
        <div className="deploy-field-grid">
          <label><span>入口类型</span><select value={draft.exposure} onChange={(event) => patchExposure(event.target.value as Exposure)}>{EXPOSURES.map((exposure) => {
            const requiredRegion = requiredRegionForExposure(exposure);
            return <option key={exposure} value={exposure} disabled={Boolean(requiredRegion && nodes.length > 0 && !hasReadyNodeInRegion(nodes, requiredRegion))}>{exposureOptionLabel(nodes, exposure)}</option>;
          })}</select></label>
          <label><span>域名</span><input value={draft.domain} disabled={draft.exposure === "none"} onChange={(event) => patch({ domain: event.target.value })} /></label>
          <label><span>容器端口</span><input value={draft.port} disabled={draft.exposure === "none"} onChange={(event) => patch({ port: event.target.value })} /></label>
          <label><span>发布端口</span><input value={draft.publishPort} disabled={draft.exposure !== "tailscale-relay"} onChange={(event) => patch({ publishPort: event.target.value })} /></label>
          <label><span>额外网络</span><textarea value={draft.networks} onChange={(event) => patch({ networks: event.target.value })} placeholder="one network per line" /></label>
          <label><span>Labels</span><textarea value={draft.labels} onChange={(event) => patch({ labels: event.target.value })} placeholder="one label per line" /></label>
        </div>
      </section>
      <section className="deploy-config-section" id="deploy-runtime">
        <header><span>03</span><h3>运行参数</h3></header>
        <div className="deploy-field-grid">
          <label><span>命令</span><input value={draft.command} onChange={(event) => patch({ command: event.target.value })} /></label>
          <label><span>CPU limit</span><input value={draft.cpuLimit} onChange={(event) => patch({ cpuLimit: event.target.value })} placeholder="0.50" /></label>
          <label><span>Memory limit</span><input value={draft.memoryLimit} onChange={(event) => patch({ memoryLimit: event.target.value })} placeholder="512M" /></label>
          <label><span>健康检查 URL</span><input value={draft.healthcheckUrl} onChange={(event) => patch({ healthcheckUrl: event.target.value })} placeholder="http://127.0.0.1:80/healthz" /></label>
          <label><span>额外挂载</span><textarea value={draft.volumes} onChange={(event) => patch({ volumes: event.target.value })} placeholder="/srv/media:/media:ro" /></label>
          <label><span>额外 storage YAML</span><textarea value={draft.storage} onChange={(event) => patch({ storage: event.target.value })} placeholder={"data:\n  storageClass: cn-nfs\n  path: app/data"} /></label>
        </div>
        <div className="service-volume-editor">
          <div className="compose-env-header">
            <strong>存储卷</strong>
            <button type="button" className="ghost" onClick={addVolumeMount}>添加卷</button>
          </div>
          {volumeMounts.length ? volumeMounts.map((volume) => (
            <article className="service-volume-row" key={volume.id}>
              <div className="deploy-field-grid compact service-volume-grid">
                <label><span>volume</span><input value={volume.name} onChange={(event) => updateVolumeMount(volume.id, { name: event.target.value })} placeholder="code-server-config" /></label>
                <label><span>挂载到</span><input value={volume.target} onChange={(event) => updateVolumeMount(volume.id, { target: event.target.value })} placeholder="/config" /></label>
                <label><span>存储模式</span><select value={volume.storageMode} onChange={(event) => updateVolumeMount(volume.id, { storageMode: event.target.value as ServiceVolumeDraft["storageMode"] })}><option value="unmanaged">unmanaged volume</option><option value="storageClass">storageClass</option></select></label>
                {volume.storageMode === "storageClass" ? (
                  <>
                    <label><span>storageClass</span><select value={volume.storageClass} onChange={(event) => updateVolumeMount(volume.id, { storageClass: event.target.value })}><option value="">选择已注册存储</option>{storageClasses.map((item) => <option value={item.name || ""} key={item.name}>{item.name}</option>)}</select></label>
                    <label><span>path</span><input value={volume.path} onChange={(event) => updateVolumeMount(volume.id, { path: event.target.value })} placeholder={`${draft.name || "app"}/${volume.name || "data"}`} /></label>
                  </>
                ) : (
                  <label><span>说明</span><input value="普通 Docker 命名卷，Luma 不接管存储后端" disabled /></label>
                )}
                <button type="button" className="ghost" onClick={() => patch({ volumeMounts: volumeMounts.filter((item) => item.id !== volume.id) })}>删除</button>
              </div>
            </article>
          )) : <p className="deploy-muted">还没有声明命名卷。需要持久化配置或数据时添加一个卷。</p>}
        </div>
        <div className="deploy-env-editor">
          <div>
            <strong>环境变量</strong>
            <div>
              <button type="button" className="ghost" onClick={() => patch({ env: [...draft.env, { id: `env-${Date.now()}`, key: "", value: "", kind: "plain" }] })}>添加变量</button>
              <button type="button" className="ghost" onClick={() => patch({ env: [...draft.env, { id: `env-secret-${Date.now()}`, key: "", value: "", kind: "secret" }] })}>添加密钥引用</button>
            </div>
          </div>
          {draft.env.map((row) => (
            <div className="deploy-env-row compose-env-row" key={row.id}>
              <input value={row.key} onChange={(event) => updateEnv(row.id, { key: event.target.value })} placeholder="NAME" />
              <select value={row.kind || "plain"} onChange={(event) => updateEnv(row.id, { kind: event.target.value as KeyValueRow["kind"] })}>
                <option value="plain">普通变量</option>
                <option value="secret">密钥引用</option>
              </select>
              <input value={row.value} onChange={(event) => updateEnv(row.id, { value: event.target.value })} placeholder={row.kind === "secret" ? "${SECRET_NAME}" : "value"} />
              <button type="button" className="ghost" onClick={() => patch({ env: draft.env.filter((item) => item.id !== row.id) })}>删除</button>
            </div>
          ))}
        </div>
      </section>
      <section className="deploy-config-section" id="deploy-advanced">
        <header><span>04</span><h3>部署开关</h3></header>
        <div className="deploy-switch-grid">
          <label className="deploy-toggle">
            <input type="checkbox" checked={draft.skipDns} onChange={(event) => patch({ skipDns: event.target.checked })} />
            <div>
              <strong>跳过 DNS</strong>
              <span>部署时不自动在 Cloudflare 上同步更新域名解析记录</span>
            </div>
          </label>
          <label className="deploy-toggle">
            <input type="checkbox" checked={draft.skipPortainer} onChange={(event) => patch({ skipPortainer: event.target.checked })} />
            <div>
              <strong>跳过 Portainer</strong>
              <span>部署时不通过 Portainer API 创建或更新 Swarm stack</span>
            </div>
          </label>
        </div>
      </section>
    </div>
  );
}
