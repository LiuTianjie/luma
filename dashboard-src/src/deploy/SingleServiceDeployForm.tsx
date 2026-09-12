import { Checkbox } from "@/components/ui/checkbox";
import { Field, FieldContent, FieldLabel, FieldDescription } from "@/components/ui/field";
import type { DashboardNode, DashboardStorageClass, Lang } from "../types";
import { Textarea } from "@/components/ui/textarea";

import type { Exposure, KeyValueRow, Region, ServiceManifestDraft, ServiceVolumeDraft } from "./types";
import { clearNodeIfIncompatible, EXPOSURES, exposureOptionLabel, hasReadyNodeInRegion, nodesForRegion, regionChoices, requiredRegionForExposure, regionOptionLabel } from "./options";
import { serviceExposureRegion } from "./yaml";
import { SelectControl } from "../components/primitives";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

export function SingleServiceDeployForm({
  lang,
  draft,
  nodes,
  storageClasses,
  regions,
  onChange,
}: {
  lang: Lang;
  draft: ServiceManifestDraft;
  nodes: DashboardNode[];
  storageClasses: DashboardStorageClass[];
  regions?: Region[];
  onChange: (draft: ServiceManifestDraft) => void;
}) {
  const zh = lang === "zh";
  const patch = (next: Partial<ServiceManifestDraft>) => onChange({ ...draft, ...next });
  const volumeMounts = draft.volumeMounts || [];
  const regionOptions = regions && regions.length ? regions : regionChoices([], nodes);
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
          storageMode: "local",
          storageClass: "",
          path: "",
        },
      ],
    });
  };
  return (
    <div className="deploy-form-stack">
      <section className="deploy-config-section" id="deploy-basic">
        <header><span>01</span><h3>{zh ? "基础配置" : "Basics"}</h3></header>
        <div className="deploy-field-grid">
          <label><span>{zh ? "服务名" : "Service name"}</span><Input value={draft.name} onChange={(event) => patch({ name: event.target.value })} /></label>
          <label><span>{zh ? "镜像" : "Image"}</span><Input value={draft.image} onChange={(event) => patch({ image: event.target.value })} /></label>
          <label>
            <span>{zh ? "区域" : "Region"}</span>
            <SelectControl
              className="min-w-0"
              value={draft.region}
              onChange={(value) => patchRegion(value as Region)}
              options={regionOptions.map((region) => ({
                value: region,
                label: regionOptionLabel(nodes, region, lang),
                disabled: nodes.length > 0 && !hasReadyNodeInRegion(nodes, region),
              }))}
            />
          </label>
          <label>
            <span>{zh ? "节点" : "Node"}</span>
            <SelectControl
              className="min-w-0"
              value={draft.node}
              onChange={(value) => patch({ node: value })}
              options={[
                { value: "", label: zh ? `自动调度到 ${draft.region} ready 节点` : `Auto-schedule to a ready ${draft.region} node` },
                ...(selectedNodeMissing ? [{ value: draft.node, label: `${draft.node} (${zh ? "当前不可用" : "currently unavailable"})`, disabled: true }] : []),
                ...nodeOptions.map((node) => ({ value: node.name || "", label: node.name })),
              ]}
            />
            <small>{zh ? "有持久化卷时，首次部署后自动固定节点，更新与重启继续使用原数据。" : "Persistent volumes pin the first deployment node. Updates and restarts reuse its data."}</small>
          </label>
          <label><span>{zh ? "副本" : "Replicas"}</span><Input type="number" min={1} value={draft.replicas} onChange={(event) => patch({ replicas: Number(event.target.value || 1) })} /></label>
          <label className="deploy-toggle"><Checkbox checked={draft.proxy} onCheckedChange={(checked) => patch({ proxy: checked })} /><span>{zh ? "启用 egress proxy" : "Enable egress proxy"}</span></label>
        </div>
      </section>
      <section className="deploy-config-section" id="deploy-network">
        <header><span>02</span><h3>{zh ? "入口与网络" : "Ingress and network"}</h3></header>
        <div className="deploy-field-grid">
          <label>
            <span>{zh ? "入口类型" : "Exposure"}</span>
            <SelectControl
              className="min-w-0"
              value={draft.exposure}
              onChange={(value) => patchExposure(value as Exposure)}
              options={EXPOSURES.map((exposure) => {
                const requiredRegion = requiredRegionForExposure(exposure);
                return {
                  value: exposure,
                  label: exposureOptionLabel(nodes, exposure, lang),
                  disabled: Boolean(requiredRegion && nodes.length > 0 && !hasReadyNodeInRegion(nodes, requiredRegion)),
                };
              })}
            />
          </label>
          <label><span>{zh ? "域名" : "Domain"}</span><Input value={draft.domain} disabled={draft.exposure === "none"} onChange={(event) => patch({ domain: event.target.value })} /></label>
          <label><span>{zh ? "容器端口" : "Container port"}</span><Input value={draft.port} disabled={draft.exposure === "none"} onChange={(event) => patch({ port: event.target.value })} /></label>
          <label><span>{zh ? "发布端口" : "Published port"}</span><Input value={draft.publishPort} disabled={!["tailscale-relay", "tcp-relay"].includes(draft.exposure)} onChange={(event) => patch({ publishPort: event.target.value })} /></label>
          <label className="deploy-field-wide"><span>{zh ? "额外网络" : "Extra networks"}</span><Textarea value={draft.networks} onChange={(event) => patch({ networks: event.target.value })} placeholder="one network per line" /></label>
          <label className="deploy-field-wide"><span>Labels</span><Textarea value={draft.labels} onChange={(event) => patch({ labels: event.target.value })} placeholder="one label per line" /></label>
        </div>
      </section>
      <section className="deploy-config-section" id="deploy-runtime">
        <header><span>03</span><h3>{zh ? "运行参数" : "Runtime"}</h3></header>
        <div className="deploy-field-grid">
          <label className="deploy-field-wide"><span>{zh ? "命令" : "Command"}</span><Input value={draft.command} onChange={(event) => patch({ command: event.target.value })} /></label>
          <label><span>CPU limit</span><Input value={draft.cpuLimit} onChange={(event) => patch({ cpuLimit: event.target.value })} placeholder="0.50" /></label>
          <label><span>Memory limit</span><Input value={draft.memoryLimit} onChange={(event) => patch({ memoryLimit: event.target.value })} placeholder="512M" /></label>
          <label><span>{zh ? "健康检查 URL" : "Healthcheck URL"}</span><Input value={draft.healthcheckUrl} onChange={(event) => patch({ healthcheckUrl: event.target.value })} placeholder="http://127.0.0.1:80/healthz" /></label>
          <label><span>{zh ? "额外挂载" : "Extra mounts"}</span><Textarea value={draft.volumes} onChange={(event) => patch({ volumes: event.target.value })} placeholder="/srv/media:/media:ro" /></label>
          {draft.storage.trim() ? <label><span>{zh ? "原有存储配置" : "Existing storage configuration"}</span><Textarea value={draft.storage} onChange={(event) => patch({ storage: event.target.value })} /></label> : null}
        </div>
        <div className="service-volume-editor">
          <div className="compose-env-header">
            <strong>{zh ? "存储卷" : "Volumes"}</strong>
            <Button variant="outline" type="button" onClick={addVolumeMount}>{zh ? "添加卷" : "Add volume"}</Button>
          </div>
          {volumeMounts.length ? volumeMounts.map((volume) => (
            <article className="service-volume-row" key={volume.id}>
              <div className="service-volume-row-main">
                <div className="deploy-field-grid compact service-volume-grid">
                  <label><span>volume</span><Input value={volume.name} onChange={(event) => updateVolumeMount(volume.id, { name: event.target.value })} placeholder="code-server-config" /></label>
                  <label><span>{zh ? "挂载到" : "Mount target"}</span><Input value={volume.target} onChange={(event) => updateVolumeMount(volume.id, { target: event.target.value })} placeholder="/config" /></label>
                  <label><span>{zh ? "存储方式" : "Storage"}</span>
                  <SelectControl
                    className="min-w-0"
                    value={volume.storageMode}
                    onChange={(value) => updateVolumeMount(volume.id, { storageMode: value as ServiceVolumeDraft["storageMode"] })}
                    options={[
                      { value: "local", label: zh ? "部署节点本地卷" : "Deployment-node volume" },
                      { value: "unmanaged", label: zh ? "使用已有命名卷" : "Existing named volume" },
                      ...(volume.storageMode === "storageClass" ? [{ value: "storageClass", label: zh ? "原有共享存储" : "Existing shared storage" }] : []),
                    ]}
                  />
                </label>
                  {volume.storageMode === "storageClass" ? (
                    <>
                      <label><span>storageClass</span>
                      <SelectControl
                        className="min-w-0"
                        value={volume.storageClass}
                        onChange={(value) => updateVolumeMount(volume.id, { storageClass: value })}
                        options={[
                          { value: "", label: zh ? "选择已注册存储" : "Select registered storage" },
                          ...storageClasses.map((item) => ({ value: item.name || "", label: item.name })),
                        ]}
                      />
                    </label>
                      <label><span>path</span><Input value={volume.path} onChange={(event) => updateVolumeMount(volume.id, { path: event.target.value })} placeholder={`${draft.name || "app"}/${volume.name || "data"}`} /></label>
                    </>
                  ) : (
                    <label><span>{zh ? "数据位置" : "Data location"}</span><Input value={zh ? "跟随部署节点，重启与更新保留数据" : "On the deployment node; retained across restarts and updates"} disabled /></label>
                  )}
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => patch({ volumeMounts: volumeMounts.filter((item) => item.id !== volume.id) })}
                >
                  {zh ? "删除" : "Remove"}
                </Button>
              </div>
            </article>
          )) : <p className="deploy-muted">{zh ? "还没有声明命名卷。需要持久化配置或数据时添加一个卷。" : "No named volumes yet. Add one when configuration or data should persist."}</p>}
        </div>
        <div className="deploy-env-editor">
          <div className="deploy-env-heading">
            <strong>{zh ? "环境变量" : "Environment variables"}</strong>
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="outline" size="sm" onClick={() => patch({ env: [...draft.env, { id: `env-${Date.now()}`, key: "", value: "", kind: "plain" }] })}>{zh ? "添加变量" : "Add variable"}</Button>
              <Button type="button" variant="outline" size="sm" onClick={() => patch({ env: [...draft.env, { id: `env-secret-${Date.now()}`, key: "", value: "", kind: "secret" }] })}>{zh ? "添加密钥引用" : "Add secret reference"}</Button>
            </div>
          </div>
          {draft.env.length === 0 && <p className="deploy-muted">{zh ? "暂无环境变量，可添加变量或引用已保存的密钥。" : "No environment variables. Add a variable or reference a saved secret."}</p>}
          {draft.env.map((row) => (
            <div className="deploy-env-row compose-env-row" key={row.id}>
              <Input value={row.key} onChange={(event) => updateEnv(row.id, { key: event.target.value })} placeholder="NAME" />
              <SelectControl
                className="min-w-0"
                value={row.kind || "plain"}
                onChange={(value) => updateEnv(row.id, { kind: value as KeyValueRow["kind"] })}
                options={[
                  { value: "plain", label: zh ? "普通变量" : "Plain variable" },
                  { value: "secret", label: zh ? "密钥引用" : "Secret reference" },
                ]}
              />
              <Input value={row.value} onChange={(event) => updateEnv(row.id, { value: event.target.value })} placeholder={row.kind === "secret" ? "${SECRET_NAME}" : "value"} />
              <Button type="button" variant="ghost" size="sm" onClick={() => patch({ env: draft.env.filter((item) => item.id !== row.id) })}>{zh ? "删除" : "Remove"}</Button>
            </div>
          ))}
        </div>
      </section>
      <section className="deploy-config-section" id="deploy-advanced">
        <header><span>04</span><h3>{zh ? "部署开关" : "Deploy options"}</h3></header>
        <div className="deploy-switch-grid">
          <Field orientation="horizontal"><Checkbox id="SingleServiceDeployForm-skipDns" checked={draft.skipDns} onCheckedChange={(checked) => patch({ skipDns: checked })} /><FieldContent><FieldLabel htmlFor="SingleServiceDeployForm-skipDns">{zh ? "跳过 DNS" : "Skip DNS"}</FieldLabel><FieldDescription>{zh ? "部署时不自动在 Cloudflare 上同步更新域名解析记录" : "Do not automatically sync Cloudflare DNS records during deploy."}</FieldDescription></FieldContent></Field>
          <Field orientation="horizontal"><Checkbox id="SingleServiceDeployForm-skipOrchestrator" checked={draft.skipOrchestrator} onCheckedChange={(checked) => patch({ skipOrchestrator: checked })} /><FieldContent><FieldLabel htmlFor="SingleServiceDeployForm-skipOrchestrator">{zh ? "跳过编排器" : "Skip orchestrator"}</FieldLabel><FieldDescription>{zh ? "只写入配置和路由，不提交 Nomad 部署" : "Write configuration and routes without submitting the Nomad deploy."}</FieldDescription></FieldContent></Field>
        </div>
      </section>
    </div>
  );
}
