import { Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { FieldGroup, FieldLegend, FieldSet } from "@/components/ui/field";
import type { DashboardNode, DashboardStorageClass, Lang } from "../types";
import type { Exposure, Region, ServiceManifestDraft, ServiceVolumeDraft } from "./types";
import { clearNodeIfIncompatible, EXPOSURES, exposureOptionLabel, hasReadyNodeInRegion, nodesForRegion, regionChoices, requiredRegionForExposure, regionOptionLabel } from "./options";
import { serviceExposureRegion } from "./yaml";
import { DeployCheckboxField, DeployEnvironmentFields, DeployFormSection, DeploySelectField, DeployTextareaField, DeployTextField } from "./DeployFormFields";

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
    <div className="flex min-w-0 flex-col gap-6">
      <DeployFormSection id="deploy-basic" title={zh ? "01 基础配置" : "01 Basics"}>
        <FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
          <DeployTextField label={zh ? "服务名" : "Service name"} value={draft.name} onChange={(event) => patch({ name: event.target.value })} error={!draft.name.trim() ? (zh ? "请输入服务名" : "Enter a service name") : undefined} />
          <DeployTextField label={zh ? "镜像" : "Image"} value={draft.image} onChange={(event) => patch({ image: event.target.value })} error={!draft.image.trim() ? (zh ? "请输入镜像" : "Enter an image") : undefined} />
          <DeploySelectField label={zh ? "区域" : "Region"} value={draft.region} onChange={(value) => patchRegion(value as Region)} options={regionOptions.map((region) => ({ value: region, label: regionOptionLabel(nodes, region, lang), disabled: nodes.length > 0 && !hasReadyNodeInRegion(nodes, region) }))} />
          <DeploySelectField label={zh ? "节点" : "Node"} value={draft.node} onChange={(value) => patch({ node: value })} options={[
            { value: "", label: zh ? `自动调度到 ${draft.region} ready 节点` : `Auto-schedule to a ready ${draft.region} node` },
            ...(selectedNodeMissing ? [{ value: draft.node, label: `${draft.node} (${zh ? "当前不可用" : "currently unavailable"})`, disabled: true }] : []),
            ...nodeOptions.map((node) => ({ value: node.name || "", label: node.name })),
          ]} description={zh ? "有持久化卷时，首次部署后自动固定节点，更新与重启继续使用原数据。" : "Persistent volumes pin the first deployment node. Updates and restarts reuse its data."} />
          <DeployTextField label={zh ? "副本" : "Replicas"} type="number" min={1} value={draft.replicas} onChange={(event) => patch({ replicas: Number(event.target.value || 1) })} error={!Number.isInteger(draft.replicas) || draft.replicas < 1 ? (zh ? "副本数必须为正整数" : "Replicas must be a positive integer") : undefined} />
          <DeployCheckboxField label={zh ? "启用 egress proxy" : "Enable egress proxy"} checked={draft.proxy} onCheckedChange={(checked) => patch({ proxy: checked })} />
        </FieldGroup>
      </DeployFormSection>
      <DeployFormSection id="deploy-network" title={zh ? "02 入口与网络" : "02 Ingress and network"}>
        <FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
          <DeploySelectField label={zh ? "入口类型" : "Exposure"} value={draft.exposure} onChange={(value) => patchExposure(value as Exposure)} options={EXPOSURES.map((exposure) => {
            const requiredRegion = requiredRegionForExposure(exposure);
            return { value: exposure, label: exposureOptionLabel(nodes, exposure, lang), disabled: Boolean(requiredRegion && nodes.length > 0 && !hasReadyNodeInRegion(nodes, requiredRegion)) };
          })} />
          <DeployTextField label={zh ? "域名" : "Domain"} value={draft.domain} disabled={draft.exposure === "none"} onChange={(event) => patch({ domain: event.target.value })} error={draft.exposure !== "none" && !draft.domain.trim() ? (zh ? "请输入域名" : "Enter a domain") : undefined} />
          <DeployTextField label={zh ? "容器端口" : "Container port"} value={draft.port} inputMode="numeric" disabled={draft.exposure === "none"} onChange={(event) => patch({ port: event.target.value })} error={draft.exposure !== "none" && (!Number.isInteger(Number(draft.port)) || Number(draft.port) < 1) ? (zh ? "请输入有效的正整数端口" : "Enter a positive integer port") : undefined} />
          <DeployTextField label={zh ? "发布端口" : "Published port"} value={draft.publishPort} inputMode="numeric" disabled={!["tailscale-relay", "tcp-relay"].includes(draft.exposure)} onChange={(event) => patch({ publishPort: event.target.value })} error={draft.publishPort.trim() && (!Number.isInteger(Number(draft.publishPort)) || Number(draft.publishPort) < 1) ? (zh ? "请输入有效的正整数端口" : "Enter a positive integer port") : undefined} />
          <DeployTextareaField wide label={zh ? "额外网络" : "Extra networks"} value={draft.networks} onChange={(event) => patch({ networks: event.target.value })} placeholder={zh ? "每行一个网络" : "One network per line"} />
          <DeployTextareaField wide label="Labels" value={draft.labels} onChange={(event) => patch({ labels: event.target.value })} placeholder={zh ? "每行一个标签" : "One label per line"} />
        </FieldGroup>
      </DeployFormSection>
      <DeployFormSection id="deploy-runtime" title={zh ? "03 运行参数" : "03 Runtime"}>
        <FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
          <DeployTextField wide label={zh ? "命令" : "Command"} value={draft.command} onChange={(event) => patch({ command: event.target.value })} />
          <DeployTextField label="CPU limit" value={draft.cpuLimit} onChange={(event) => patch({ cpuLimit: event.target.value })} placeholder="0.50" />
          <DeployTextField label="Memory limit" value={draft.memoryLimit} onChange={(event) => patch({ memoryLimit: event.target.value })} placeholder="512M" />
          <DeployTextField label={zh ? "健康检查 URL" : "Healthcheck URL"} value={draft.healthcheckUrl} onChange={(event) => patch({ healthcheckUrl: event.target.value })} placeholder="http://127.0.0.1:80/healthz" />
          <DeployTextareaField label={zh ? "额外挂载" : "Extra mounts"} value={draft.volumes} onChange={(event) => patch({ volumes: event.target.value })} placeholder="/srv/media:/media:ro" />
          {draft.storage.trim() ? <DeployTextareaField label={zh ? "原有存储配置" : "Existing storage configuration"} value={draft.storage} onChange={(event) => patch({ storage: event.target.value })} /> : null}
        </FieldGroup>
        <FieldSet>
          <FieldLegend>{zh ? "存储卷" : "Volumes"}</FieldLegend>
          <Button variant="outline" type="button" className="w-fit" onClick={addVolumeMount}><Plus data-icon="inline-start" />{zh ? "添加卷" : "Add volume"}</Button>
          {volumeMounts.length ? <FieldGroup>{volumeMounts.map((volume, index) => <FieldSet key={volume.id}>
            <FieldLegend variant="label">{volume.name || (zh ? `卷 ${index + 1}` : `Volume ${index + 1}`)}</FieldLegend>
            <FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
              <DeployTextField label={zh ? "卷名" : "Volume name"} value={volume.name} onChange={(event) => updateVolumeMount(volume.id, { name: event.target.value })} placeholder="code-server-config" error={!volume.name.trim() && volume.target.trim() ? (zh ? "请输入卷名" : "Enter a volume name") : undefined} />
              <DeployTextField label={zh ? "挂载到" : "Mount target"} value={volume.target} onChange={(event) => updateVolumeMount(volume.id, { target: event.target.value })} placeholder="/config" error={volume.name.trim() && !volume.target.trim() ? (zh ? "请输入挂载目标" : "Enter a mount target") : undefined} />
              <DeploySelectField label={zh ? "存储方式" : "Storage"} value={volume.storageMode} onChange={(value) => updateVolumeMount(volume.id, { storageMode: value as ServiceVolumeDraft["storageMode"] })} options={[
                { value: "local", label: zh ? "部署节点本地卷" : "Deployment-node volume" },
                { value: "unmanaged", label: zh ? "使用已有命名卷" : "Existing named volume" },
                ...(volume.storageMode === "storageClass" ? [{ value: "storageClass", label: zh ? "原有共享存储" : "Existing shared storage" }] : []),
              ]} />
              {volume.storageMode === "storageClass" ? <>
                <DeploySelectField label="storageClass" value={volume.storageClass} onChange={(value) => updateVolumeMount(volume.id, { storageClass: value })} options={[
                  { value: "", label: zh ? "选择已注册存储" : "Select registered storage" },
                  ...storageClasses.map((item) => ({ value: item.name || "", label: item.name })),
                ]} error={volume.name.trim() && !volume.storageClass.trim() ? (zh ? "请选择存储类" : "Select a storage class") : undefined} />
                <DeployTextField label="path" value={volume.path} onChange={(event) => updateVolumeMount(volume.id, { path: event.target.value })} placeholder={`${draft.name || "app"}/${volume.name || "data"}`} />
              </> : <DeployTextField label={zh ? "数据位置" : "Data location"} value={zh ? "跟随部署节点，重启与更新保留数据" : "On the deployment node; retained across restarts and updates"} disabled />}
            </FieldGroup>
            <Button type="button" variant="ghost" size="sm" className="self-end" aria-label={zh ? `删除卷 ${volume.name || index + 1}` : `Remove volume ${volume.name || index + 1}`} onClick={() => patch({ volumeMounts: volumeMounts.filter((item) => item.id !== volume.id) })}><Trash2 data-icon="inline-start" />{zh ? "删除" : "Remove"}</Button>
          </FieldSet>)}</FieldGroup> : <Empty><EmptyHeader><EmptyTitle>{zh ? "暂无存储卷" : "No volumes"}</EmptyTitle><EmptyDescription>{zh ? "需要持久化配置或数据时添加一个卷。" : "Add a volume when configuration or data should persist."}</EmptyDescription></EmptyHeader></Empty>}
        </FieldSet>
        <DeployEnvironmentFields lang={lang} label={zh ? "环境变量" : "Environment variables"} rows={draft.env} onChange={(env) => patch({ env })} />
      </DeployFormSection>
      <DeployFormSection id="deploy-advanced" title={zh ? "04 部署开关" : "04 Deploy options"}>
        <FieldSet><FieldLegend className="sr-only">{zh ? "部署选项" : "Deployment options"}</FieldLegend><FieldGroup>
          <DeployCheckboxField label={zh ? "跳过 DNS" : "Skip DNS"} checked={draft.skipDns} onCheckedChange={(checked) => patch({ skipDns: checked })} description={zh ? "部署时不自动在 Cloudflare 上同步更新域名解析记录" : "Do not automatically sync Cloudflare DNS records during deploy."} />
          <DeployCheckboxField label={zh ? "跳过编排器" : "Skip orchestrator"} checked={draft.skipOrchestrator} onCheckedChange={(checked) => patch({ skipOrchestrator: checked })} description={zh ? "只写入配置和路由，不提交 Nomad 部署" : "Write configuration and routes without submitting the Nomad deploy."} />
        </FieldGroup></FieldSet>
      </DeployFormSection>
    </div>
  );
}
