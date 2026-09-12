import { FileCode2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldDescription, FieldGroup, FieldLegend, FieldSet, FieldTitle } from "@/components/ui/field";
import type { DashboardNode, DashboardStorageClass, Lang } from "../types";
import type { ComposeDeploymentDraft, ComposeServiceDraft, ComposeVolumeDraft, Exposure, Region } from "./types";
import { clearNodeIfIncompatible, EXPOSURES, exposureOptionLabel, hasReadyNodeInRegion, nodesForRegion, regionChoices, requiredRegionForExposure, regionOptionLabel } from "./options";
import { defaultLocalVolumePath, updateComposeServiceExposure } from "./yaml";
import { DeployCheckboxField, DeployEnvironmentFields, DeployFormSection, DeploySelectField, DeployTextField } from "./DeployFormFields";

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
  const updateVolume = (name: string, next: Partial<ComposeVolumeDraft>) => {
    patch({ volumes: draft.volumes.map((volume) => volume.name === name ? { ...volume, ...next } : volume) });
  };
  return (
    <div className="flex min-w-0 flex-col gap-6">
      <DeployFormSection id="compose-basic" title={zh ? "01 应用配置" : "01 Application config"}>
        <FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
          <DeployTextField label={zh ? "应用名" : "Application name"} value={draft.name} onChange={(event) => patch({ name: event.target.value })} error={!draft.name.trim() ? (zh ? "请输入应用名" : "Enter an application name") : undefined} />
          <DeployTextField label={zh ? "Compose 文件名" : "Compose file name"} value={draft.composeFileName} onChange={(event) => patch({ composeFileName: event.target.value })} description={zh ? "这里只改提交文件名，Compose 内容在 YAML 文件里编辑。" : "This only changes the submitted file name. Edit Compose content in the YAML view."} />
          <DeploySelectField label={zh ? "默认区域" : "Default region"} value={draft.region} onChange={(value) => updateDefaultRegion(value as Region)} options={regionOptions.map((region) => ({ value: region, label: regionOptionLabel(nodes, region, lang), disabled: nodes.length > 0 && !hasReadyNodeInRegion(nodes, region) }))} />
          <Field>
            <FieldTitle>{zh ? "Compose 内容" : "Compose content"}</FieldTitle>
            <FieldDescription>{zh ? "在 YAML 视图中编辑完整 docker-compose.yml" : "Edit the full docker-compose.yml in the YAML view"}</FieldDescription>
            <Button type="button" variant="outline" className="w-fit" onClick={onEditYaml}><FileCode2 data-icon="inline-start" />{zh ? "编辑 docker-compose.yml" : "Edit docker-compose.yml"}</Button>
          </Field>
        </FieldGroup>
      </DeployFormSection>
      <DeployFormSection id="compose-services" title={zh ? "02 服务入口" : "02 Service ingress"}>
        {draft.services.length ? draft.services.map((service) => {
          const effectiveRegion = service.region || draft.region;
          const nodeOptions = nodesForRegion(nodes, effectiveRegion);
          const selectedNodeMissing = service.node && !nodeOptions.some((node) => node.name === service.node);
          return <FieldSet key={service.name}>
            <FieldLegend>{service.name}</FieldLegend>
            <FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
              <DeploySelectField label={zh ? "入口" : "Exposure"} value={service.exposure} onChange={(value) => updateServiceExposureSafe(service, value as Exposure)} options={EXPOSURES.map((exposure) => {
                const requiredRegion = requiredRegionForExposure(exposure);
                return { value: exposure, label: exposureOptionLabel(nodes, exposure, lang), disabled: Boolean(requiredRegion && nodes.length > 0 && !hasReadyNodeInRegion(nodes, requiredRegion)) };
              })} />
              <DeploySelectField label={zh ? "区域" : "Region"} value={service.region} onChange={(value) => updateServiceRegion(service, value as Region | "")} options={[
                { value: "", label: zh ? `默认 (${draft.region})` : `Default (${draft.region})` },
                ...regionOptions.map((region) => ({ value: region, label: regionOptionLabel(nodes, region, lang), disabled: nodes.length > 0 && !hasReadyNodeInRegion(nodes, region) })),
              ]} />
              <DeploySelectField label={zh ? "节点" : "Node"} value={service.node} onChange={(value) => updateService(service.name, { node: value })} options={[
                { value: "", label: zh ? `自动调度到 ${effectiveRegion} ready 节点` : `Auto-schedule to a ready ${effectiveRegion} node` },
                ...(selectedNodeMissing ? [{ value: service.node, label: `${service.node} (${zh ? "当前不可用" : "currently unavailable"})`, disabled: true }] : []),
                ...nodeOptions.map((node) => ({ value: node.name || "", label: node.name })),
              ]} />
              <DeployTextField label={zh ? "域名" : "Domain"} value={service.domain} disabled={service.exposure === "none"} onChange={(event) => updateService(service.name, { domain: event.target.value })} error={service.exposure !== "none" && !service.domain.trim() ? (zh ? "请输入域名" : "Enter a domain") : undefined} />
              <DeployTextField label={zh ? "容器端口" : "Container port"} value={service.port} inputMode="numeric" disabled={service.exposure === "none"} onChange={(event) => updateService(service.name, { port: event.target.value })} error={service.exposure !== "none" && (!Number.isInteger(Number(service.port)) || Number(service.port) < 1) ? (zh ? "请输入有效的正整数端口" : "Enter a positive integer port") : undefined} />
              <DeployTextField label={zh ? "发布端口" : "Published port"} value={service.publishPort} inputMode="numeric" disabled={!["tailscale-relay", "tcp-relay"].includes(service.exposure)} onChange={(event) => updateService(service.name, { publishPort: event.target.value })} error={service.publishPort.trim() && (!Number.isInteger(Number(service.publishPort)) || Number(service.publishPort) < 1) ? (zh ? "请输入有效的正整数端口" : "Enter a positive integer port") : undefined} />
              <DeployTextField label={zh ? "副本" : "Replicas"} type="number" min={1} value={service.replicas} onChange={(event) => updateService(service.name, { replicas: Number(event.target.value || 1) })} error={!Number.isInteger(service.replicas) || service.replicas < 1 ? (zh ? "副本数必须为正整数" : "Replicas must be a positive integer") : undefined} />
              <DeployCheckboxField label="egress proxy" checked={service.proxy} onCheckedChange={(checked) => updateService(service.name, { proxy: checked })} />
            </FieldGroup>
          </FieldSet>;
        }) : <Empty><EmptyHeader><EmptyTitle>{zh ? "暂无服务" : "No services"}</EmptyTitle><EmptyDescription>{zh ? "先在 docker-compose.yml 中声明服务。" : "Declare services in docker-compose.yml first."}</EmptyDescription></EmptyHeader><EmptyContent><Button variant="outline" onClick={onEditYaml}><FileCode2 data-icon="inline-start" />{zh ? "编辑 YAML" : "Edit YAML"}</Button></EmptyContent></Empty>}
      </DeployFormSection>
      <DeployFormSection id="compose-env" title={zh ? "03 环境变量与密钥" : "03 Environment and secrets"}>
        {draft.services.length ? draft.services.map((service) => <DeployEnvironmentFields key={service.name} lang={lang} label={service.name} rows={service.env || []} onChange={(env) => updateService(service.name, { env })} />) : <Empty><EmptyHeader><EmptyTitle>{zh ? "暂无服务环境变量" : "No service environment"}</EmptyTitle><EmptyDescription>{zh ? "先在 docker-compose.yml 中声明服务，再配置服务环境变量。" : "Declare services in docker-compose.yml before configuring service environment variables."}</EmptyDescription></EmptyHeader></Empty>}
      </DeployFormSection>
      <DeployFormSection id="compose-storage" title={zh ? "04 存储卷" : "04 Volumes"}>
        {draft.volumes.length ? draft.volumes.map((volume) => <FieldSet key={volume.name}>
          <FieldLegend>{volume.name}</FieldLegend>
          {volume.target ? <FieldDescription>{volume.target}</FieldDescription> : null}
          <FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
            <DeploySelectField label={zh ? "存储方式" : "Storage"} value={volume.storageMode} onChange={(value) => updateVolume(volume.name, { storageMode: value as ComposeVolumeDraft["storageMode"] })} options={[
              { value: "local", label: zh ? "部署节点本地目录" : "Deployment-node directory" },
              { value: "unmanaged", label: zh ? "使用已有命名卷" : "Existing named volume" },
              ...(volume.storageMode === "storageClass" ? [{ value: "storageClass", label: zh ? "原有共享存储" : "Existing shared storage" }] : []),
            ]} />
            {volume.storageMode === "storageClass" ? <DeploySelectField label="storageClass" value={volume.storageClass} onChange={(value) => updateVolume(volume.name, { storageClass: value })} options={[
              { value: "", label: zh ? "选择已注册存储" : "Select registered storage" },
              ...storageClasses.map((item) => ({ value: item.name || "", label: item.name })),
            ]} error={!volume.storageClass ? (zh ? "请选择存储类" : "Select a storage class") : undefined} /> : volume.storageMode === "local" ? <>
              <DeployTextField label={zh ? "数据位置" : "Data location"} value={volume.localNode || (zh ? "跟随部署节点并固定" : "Pinned to the deployment node")} disabled />
              <DeployTextField label={zh ? "本地路径（可选）" : "Local path (optional)"} value={volume.localPath} onChange={(event) => updateVolume(volume.name, { localPath: event.target.value })} placeholder={defaultLocalVolumePath(draft.name, volume.name)} />
            </> : <DeployTextField label={zh ? "说明" : "Note"} value={zh ? "保留原卷名，固定到部署节点" : "Preserve the volume name and pin its deployment node"} disabled />}
          </FieldGroup>
        </FieldSet>) : <Empty><EmptyHeader><EmptyTitle>{zh ? "暂无存储卷" : "No volumes"}</EmptyTitle><EmptyDescription>{zh ? "当前 Compose 模板没有声明命名卷。" : "This Compose template does not declare named volumes."}</EmptyDescription></EmptyHeader></Empty>}
      </DeployFormSection>
      <DeployFormSection id="compose-advanced" title={zh ? "05 部署开关" : "05 Deploy options"}>
        <FieldSet><FieldLegend className="sr-only">{zh ? "部署选项" : "Deployment options"}</FieldLegend><FieldGroup>
          <DeployCheckboxField label={zh ? "跳过 DNS" : "Skip DNS"} checked={draft.skipDns} onCheckedChange={(checked) => patch({ skipDns: checked })} description={zh ? "部署时不自动在 Cloudflare 上同步更新域名解析记录" : "Do not automatically sync Cloudflare DNS records during deploy."} />
          <DeployCheckboxField label={zh ? "跳过编排器" : "Skip orchestrator"} checked={draft.skipOrchestrator} onCheckedChange={(checked) => patch({ skipOrchestrator: checked })} description={zh ? "只写入配置和路由，不提交 Nomad 部署" : "Write configuration and routes without submitting the Nomad deploy."} />
        </FieldGroup></FieldSet>
      </DeployFormSection>
    </div>
  );
}
