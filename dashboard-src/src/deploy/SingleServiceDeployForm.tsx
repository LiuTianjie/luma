import type { DashboardNode } from "../types";
import type { Exposure, KeyValueRow, Region, ServiceManifestDraft } from "./types";
import { serviceExposureRegion } from "./yaml";

const exposures: Exposure[] = ["none", "cn-edge", "external-edge", "tailscale-relay", "cloudflare-tunnel"];
const regions: Region[] = ["cn", "global", "home"];

export function SingleServiceDeployForm({
  draft,
  nodes,
  onChange,
}: {
  draft: ServiceManifestDraft;
  nodes: DashboardNode[];
  onChange: (draft: ServiceManifestDraft) => void;
}) {
  const patch = (next: Partial<ServiceManifestDraft>) => onChange({ ...draft, ...next });
  const updateEnv = (id: string, next: Partial<KeyValueRow>) => {
    patch({ env: draft.env.map((row) => row.id === id ? { ...row, ...next } : row) });
  };
  return (
    <div className="deploy-form-stack">
      <section className="deploy-config-section" id="deploy-basic">
        <header><span>01</span><h3>基础配置</h3></header>
        <div className="deploy-field-grid">
          <label><span>服务名</span><input value={draft.name} onChange={(event) => patch({ name: event.target.value })} /></label>
          <label><span>镜像</span><input value={draft.image} onChange={(event) => patch({ image: event.target.value })} /></label>
          <label><span>区域</span><select value={draft.region} onChange={(event) => patch({ region: event.target.value as Region })}>{regions.map((region) => <option key={region}>{region}</option>)}</select></label>
          <label><span>节点</span><select value={draft.node} onChange={(event) => patch({ node: event.target.value })}><option value="">自动调度</option>{nodes.map((node) => <option value={node.name || ""} key={node.name}>{node.name}</option>)}</select></label>
          <label><span>副本</span><input type="number" min={1} value={draft.replicas} onChange={(event) => patch({ replicas: Number(event.target.value || 1) })} /></label>
          <label className="deploy-toggle"><input type="checkbox" checked={draft.proxy} onChange={(event) => patch({ proxy: event.target.checked })} /><span>启用 egress proxy</span></label>
        </div>
      </section>
      <section className="deploy-config-section" id="deploy-network">
        <header><span>02</span><h3>入口与网络</h3></header>
        <div className="deploy-field-grid">
          <label><span>入口类型</span><select value={draft.exposure} onChange={(event) => {
            const exposure = event.target.value as Exposure;
            patch({ exposure, region: serviceExposureRegion(exposure, draft.region) });
          }}>{exposures.map((exposure) => <option key={exposure}>{exposure}</option>)}</select></label>
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
          <label><span>卷挂载</span><textarea value={draft.volumes} onChange={(event) => patch({ volumes: event.target.value })} placeholder="data:/data" /></label>
          <label><span>CPU limit</span><input value={draft.cpuLimit} onChange={(event) => patch({ cpuLimit: event.target.value })} placeholder="0.50" /></label>
          <label><span>Memory limit</span><input value={draft.memoryLimit} onChange={(event) => patch({ memoryLimit: event.target.value })} placeholder="512M" /></label>
          <label><span>健康检查 URL</span><input value={draft.healthcheckUrl} onChange={(event) => patch({ healthcheckUrl: event.target.value })} placeholder="http://127.0.0.1:80/healthz" /></label>
        </div>
        <div className="deploy-env-editor">
          <div><strong>环境变量</strong><button type="button" className="ghost" onClick={() => patch({ env: [...draft.env, { id: `env-${Date.now()}`, key: "", value: "" }] })}>添加</button></div>
          {draft.env.map((row) => (
            <div className="deploy-env-row" key={row.id}>
              <input value={row.key} onChange={(event) => updateEnv(row.id, { key: event.target.value })} placeholder="NAME" />
              <input value={row.value} onChange={(event) => updateEnv(row.id, { value: event.target.value })} placeholder="${SECRET_NAME} 或普通值" />
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
