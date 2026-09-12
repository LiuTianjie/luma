import { Checkbox } from "@/components/ui/checkbox";
import { Field, FieldContent, FieldLabel, FieldDescription } from "@/components/ui/field";
import type { DashboardNode, DashboardStorageClass, Lang } from "../types";
import type { ComposeDeploymentDraft, ComposeServiceDraft, ComposeVolumeDraft, Exposure, KeyValueRow, Region } from "./types";
import { clearNodeIfIncompatible, EXPOSURES, exposureOptionLabel, hasReadyNodeInRegion, nodesForRegion, regionChoices, requiredRegionForExposure, regionOptionLabel } from "./options";
import { defaultLocalVolumePath, updateComposeServiceExposure } from "./yaml";
import { SelectControl } from "../components/primitives";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

export function ComposeDeployForm({
  lang,
  draft,
  nodes,
  storageClasses,
  regions,
  onChange,
  onEditYaml,
}: {
  lang: Lang;
  draft: ComposeDeploymentDraft;
  nodes: DashboardNode[];
  storageClasses: DashboardStorageClass[];
  regions?: Region[];
  onChange: (draft: ComposeDeploymentDraft) => void;
  onEditYaml: () => void;
}) {
  const zh = lang === "zh";
  const regionOptions = regions && regions.length ? regions : regionChoices([], nodes);
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
          <label><span>{zh ? "应用名" : "Application name"}</span><Input value={draft.name} onChange={(event) => patch({ name: event.target.value })} /></label>
          <label className="compose-file-field">
            <span>{zh ? "Compose 文件名" : "Compose file name"}</span>
            <Input value={draft.composeFileName} onChange={(event) => patch({ composeFileName: event.target.value })} />
            <small>{zh ? "这里只改提交文件名，Compose 内容在 YAML 文件里编辑。" : "This only changes the submitted file name. Edit Compose content in the YAML view."}</small>
          </label>
          <div className="compose-yaml-shortcut">
            <div className="compose-yaml-shortcut-copy">
              <span>{zh ? "Compose 内容" : "Compose content"}</span>
              <small className="deploy-muted">{zh ? "在 YAML 视图中编辑完整 docker-compose.yml" : "Edit the full docker-compose.yml in the YAML view"}</small>
            </div>
            <Button type="button" variant="outline" onClick={onEditYaml}>{zh ? "编辑 docker-compose.yml" : "Edit docker-compose.yml"}</Button>
          </div>
          <label><span>{zh ? "默认区域" : "Default region"}</span>
            <SelectControl
              className="min-w-0"
              value={draft.region}
              onChange={(value) => updateDefaultRegion(value as Region)}
              options={regionOptions.map((region) => ({
                value: region,
                label: regionOptionLabel(nodes, region, lang),
                disabled: nodes.length > 0 && !hasReadyNodeInRegion(nodes, region),
              }))}
            />
          </label>
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
                <label><span>{zh ? "入口" : "Exposure"}</span><SelectControl
                  className="min-w-0"
                  value={service.exposure}
                  onChange={(value) => updateServiceExposureSafe(service, value as Exposure)}
                  options={EXPOSURES.map((exposure) => {
                    const requiredRegion = requiredRegionForExposure(exposure);
                    return {
                      value: exposure,
                      label: exposureOptionLabel(nodes, exposure, lang),
                      disabled: Boolean(requiredRegion && nodes.length > 0 && !hasReadyNodeInRegion(nodes, requiredRegion)),
                    };
                  })}
                /></label>
                <label><span>{zh ? "区域" : "Region"}</span><SelectControl
                  className="min-w-0"
                  value={service.region}
                  onChange={(value) => updateServiceRegion(service, value as Region | "")}
                  options={[
                    { value: "", label: zh ? `默认 (${draft.region})` : `Default (${draft.region})` },
                    ...regionOptions.map((region) => ({
                      value: region,
                      label: regionOptionLabel(nodes, region, lang),
                      disabled: nodes.length > 0 && !hasReadyNodeInRegion(nodes, region),
                    })),
                  ]}
                /></label>
                <label>
                  <span>{zh ? "节点" : "Node"}</span>
                  <SelectControl
                    className="min-w-0"
                    value={service.node}
                    onChange={(value) => updateService(service.name, { node: value })}
                    options={[
                      { value: "", label: zh ? `自动调度到 ${effectiveRegion} ready 节点` : `Auto-schedule to a ready ${effectiveRegion} node` },
                      ...(selectedNodeMissing ? [{ value: service.node, label: `${service.node} (${zh ? "当前不可用" : "currently unavailable"})`, disabled: true }] : []),
                      ...nodeOptions.map((node) => ({ value: node.name || "", label: node.name })),
                    ]}
                  />
                </label>
                <label><span>{zh ? "域名" : "Domain"}</span><Input value={service.domain} disabled={service.exposure === "none"} onChange={(event) => updateService(service.name, { domain: event.target.value })} /></label>
                <label><span>{zh ? "容器端口" : "Container port"}</span><Input value={service.port} disabled={service.exposure === "none"} onChange={(event) => updateService(service.name, { port: event.target.value })} /></label>
                <label><span>{zh ? "发布端口" : "Published port"}</span><Input value={service.publishPort} disabled={!["tailscale-relay", "tcp-relay"].includes(service.exposure)} onChange={(event) => updateService(service.name, { publishPort: event.target.value })} /></label>
                <label><span>{zh ? "副本" : "Replicas"}</span><Input type="number" min={1} value={service.replicas} onChange={(event) => updateService(service.name, { replicas: Number(event.target.value || 1) })} /></label>
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
                  <Button type="button" variant="outline" size="sm" onClick={() => addEnv(service)}>{zh ? "添加变量" : "Add variable"}</Button>
                  <Button type="button" variant="outline" size="sm" onClick={() => addEnv(service, "secret")}>{zh ? "添加密钥引用" : "Add secret reference"}</Button>
                </div>
              </div>
              <p className="deploy-muted">{zh ? <>普通变量会写入 docker-compose.yml；密钥只填写 ${"{NAME}"} 引用，明文请先存入 Luma Control。</> : <>Plain variables are written to docker-compose.yml. Secrets must use ${"{NAME}"} references; store plaintext secrets in Luma Control first.</>}</p>
              {(service.env || []).length ? (service.env || []).map((row) => (
                <div className="deploy-env-row compose-env-row" key={row.id}>
                  <Input value={row.key} onChange={(event) => updateEnv(service.name, row.id, { key: event.target.value })} placeholder="NAME" />
                  <SelectControl
                    className="min-w-0"
                    value={row.kind || "plain"}
                    onChange={(value) => updateEnv(service.name, row.id, { kind: value as KeyValueRow["kind"] })}
                    options={[
                      { value: "plain", label: zh ? "普通变量" : "Plain variable" },
                      { value: "secret", label: zh ? "密钥引用" : "Secret reference" },
                    ]}
                  />
                  <Input
                    value={row.value}
                    onChange={(event) => updateEnv(service.name, row.id, { value: event.target.value })}
                    placeholder={row.kind === "secret" ? "${DATABASE_URL}" : "value"}
                  />
                  <Button type="button" variant="ghost" size="sm" onClick={() => removeEnv(service, row.id)}>{zh ? "删除" : "Remove"}</Button>
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
                <label><span>{zh ? "存储方式" : "Storage"}</span><SelectControl
                  className="min-w-0"
                  value={volume.storageMode}
                  onChange={(value) => updateVolume(volume.name, { storageMode: value as ComposeVolumeDraft["storageMode"] })}
                  options={[
                    { value: "local", label: zh ? "部署节点本地目录" : "Deployment-node directory" },
                    { value: "unmanaged", label: zh ? "使用已有命名卷" : "Existing named volume" },
                    ...(volume.storageMode === "storageClass" ? [{ value: "storageClass", label: zh ? "原有共享存储" : "Existing shared storage" }] : []),
                  ]}
                /></label>
                {volume.storageMode === "storageClass" ? (
                  <label><span>storageClass</span><SelectControl
                    className="min-w-0"
                    value={volume.storageClass}
                    onChange={(value) => updateVolume(volume.name, { storageClass: value })}
                    options={[
                      { value: "", label: zh ? "选择已注册存储" : "Select registered storage" },
                      ...storageClasses.map((item) => ({ value: item.name || "", label: item.name })),
                    ]}
                  /></label>
                ) : volume.storageMode === "local" ? (
                  <>
                    <label><span>{zh ? "数据位置" : "Data location"}</span><Input value={volume.localNode || (zh ? "跟随部署节点并固定" : "Pinned to the deployment node")} disabled /></label>
                    <label><span>{zh ? "本地路径（可选）" : "Local path (optional)"}</span><Input value={volume.localPath} onChange={(event) => updateVolume(volume.name, { localPath: event.target.value })} placeholder={defaultLocalVolumePath(draft.name, volume.name)} /></label>
                  </>
                ) : (
                  <label><span>{zh ? "说明" : "Note"}</span><Input value={zh ? "保留原卷名，固定到部署节点" : "Preserve the volume name and pin its deployment node"} disabled /></label>
                )}
              </div>
            </article>
          )) : <p className="deploy-muted">{zh ? "当前 Compose 模板没有声明命名卷。" : "This Compose template does not declare named volumes."}</p>}
        </div>
      </section>
      <section className="deploy-config-section" id="compose-advanced">
        <header><span>05</span><h3>{zh ? "部署开关" : "Deploy options"}</h3></header>
        <div className="deploy-switch-grid">
          <Field orientation="horizontal"><Checkbox id="ComposeDeployForm-skipDns" checked={draft.skipDns} onCheckedChange={(checked) => patch({ skipDns: checked })} /><FieldContent><FieldLabel htmlFor="ComposeDeployForm-skipDns">{zh ? "跳过 DNS" : "Skip DNS"}</FieldLabel><FieldDescription>{zh ? "部署时不自动在 Cloudflare 上同步更新域名解析记录" : "Do not automatically sync Cloudflare DNS records during deploy."}</FieldDescription></FieldContent></Field>
          <Field orientation="horizontal"><Checkbox id="ComposeDeployForm-skipOrchestrator" checked={draft.skipOrchestrator} onCheckedChange={(checked) => patch({ skipOrchestrator: checked })} /><FieldContent><FieldLabel htmlFor="ComposeDeployForm-skipOrchestrator">{zh ? "跳过编排器" : "Skip orchestrator"}</FieldLabel><FieldDescription>{zh ? "只写入配置和路由，不提交 Nomad 部署" : "Write configuration and routes without submitting the Nomad deploy."}</FieldDescription></FieldContent></Field>
        </div>
      </section>
    </div>
  );
}
