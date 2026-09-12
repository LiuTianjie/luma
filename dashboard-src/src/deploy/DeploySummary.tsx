import { AlertCircle, CheckCircle2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { StepLog } from "./StepLog";
import type { SubmissionSummary } from "./submissionSummary";
import type { Lang } from "../types";
import type { ComposeDeploymentDraft, DeployMode, DeployPreviewResult, DeployStep, DeploymentHealth, ServiceManifestDraft } from "./types";

function compact(values: Array<string | number | undefined | null | false>) {
  return values.filter((value) => value !== undefined && value !== null && value !== false && value !== "").join(" / ") || "-";
}

export function DeploySummary({
  lang,
  mode,
  serviceDraft,
  composeDraft,
  preview,
  steps,
  errors,
  submission,
  health,
}: {
  lang: Lang;
  mode: DeployMode;
  serviceDraft: ServiceManifestDraft;
  composeDraft: ComposeDeploymentDraft;
  preview: DeployPreviewResult | null;
  steps: DeployStep[];
  errors: string[];
  submission?: SubmissionSummary | null;
  health?: DeploymentHealth[];
}) {
  const zh = lang === "zh";
  const publicTargets = mode === "service"
    ? serviceDraft.exposure === "none" ? [] : [`${serviceDraft.domain}:${serviceDraft.port}`]
    : composeDraft.services.filter((service) => service.exposure !== "none").map((service) => `${service.name} -> ${service.domain}:${service.port}`);
  const storage = mode === "compose"
    ? composeDraft.volumes.map((volume) => volume.storageMode === "storageClass" ? `${volume.name}:${volume.storageClass || (zh ? "未选择" : "not selected")}` : `${volume.name}: ${volume.localNode || (zh ? "部署节点本地目录" : "deployment-node directory")}`)
    : (serviceDraft.volumeMounts || []).map((volume) => volume.storageMode === "storageClass" ? `${volume.name}:${volume.storageClass || (zh ? "未选择" : "not selected")}` : `${volume.name}: ${zh ? "部署节点本地卷" : "deployment-node volume"}`);
  const previewWarnings = preview ? [...(preview.warnings || []), ...(preview.storage?.warnings || [])] : [];
  const requirementGroups = preview?.requirements
    ? Array.isArray(preview.requirements)
      ? []
      : "checks" in preview.requirements
        ? [preview.requirements]
        : Object.values(preview.requirements)
    : [];
  const requirementFailures = requirementGroups.flatMap((group) => (group?.checks || []).filter((check: { required?: boolean; status?: string }) => check.required && check.status !== "ready"));
  const summaryState = errors.length
    ? (zh ? "待修正" : "Blocked")
    : preview
      ? (zh ? "已校验" : "Validated")
      : (zh ? "草稿" : "Draft");
  const summaryRows = [
    [zh ? "类型" : "Type", mode === "service" ? (zh ? "单服务" : "Single service") : (zh ? "Compose 应用" : "Compose app")],
    [zh ? "调度" : "Placement", submission ? compact([submission.region, `${submission.services.length} ${zh ? "个服务" : "services"}`]) : mode === "service" ? compact([serviceDraft.region, serviceDraft.node]) : compact([composeDraft.region, `${composeDraft.services.length} ${zh ? "个服务" : "services"}`])],
    [zh ? "入口" : "Ingress", (submission?.ingress || publicTargets).length ? (submission?.ingress || publicTargets).join(", ") : (zh ? "内部服务" : "Internal only")],
    [zh ? "存储" : "Storage", (submission?.volumes || storage).length ? (submission?.volumes || storage).join(", ") : (zh ? "无托管卷" : "No managed volumes")],
  ];
  return <aside className="flex min-w-0 flex-col gap-6 lg:sticky lg:top-6" aria-label={zh ? "部署摘要" : "Deployment summary"}>
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>{zh ? "部署摘要" : "Deployment summary"}</CardTitle>
        <CardDescription className="wrap-anywhere">{submission?.name || (mode === "service" ? serviceDraft.name : composeDraft.name)}</CardDescription>
        <CardAction><Badge variant={errors.length ? "destructive" : "secondary"}>{summaryState}</Badge></CardAction>
      </CardHeader>
      <CardContent><Table><TableBody>{summaryRows.map(([label, value]) => <TableRow key={label}><TableHead scope="row" className="w-20 align-top">{label}</TableHead><TableCell className="whitespace-normal wrap-anywhere">{value}</TableCell></TableRow>)}</TableBody></Table></CardContent>
    </Card>
    {preview ? <Card className="min-w-0">
      <CardHeader><CardTitle>{zh ? "校验结果" : "Validation result"}</CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Table><TableBody>
          <TableRow><TableHead scope="row">{zh ? "生成产物" : "Artifacts"}</TableHead><TableCell>{preview.artifacts?.length || 0}</TableCell></TableRow>
          <TableRow><TableHead scope="row">{zh ? "提示" : "Warnings"}</TableHead><TableCell>{previewWarnings.length}</TableCell></TableRow>
          <TableRow><TableHead scope="row">{zh ? "初始化动作" : "Init actions"}</TableHead><TableCell className="whitespace-normal wrap-anywhere">{requirementGroups.flatMap((group) => group?.init || []).join(", ") || (zh ? "无" : "None")}</TableCell></TableRow>
        </TableBody></Table>
        {preview.artifacts?.length ? <ul className="flex list-disc flex-col gap-2 pl-4">{preview.artifacts.map((artifact) => <li className="wrap-anywhere" key={`${artifact.kind}-${artifact.path}`}>{artifact.kind}: {artifact.path}</li>)}</ul> : null}
        {previewWarnings.length ? <Alert><AlertCircle /><AlertTitle>{zh ? "校验提示" : "Validation warnings"}</AlertTitle><AlertDescription><ul className="flex list-disc flex-col gap-2 pl-4">{previewWarnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}</ul></AlertDescription></Alert> : null}
        {requirementFailures.length ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "部署前置条件未满足" : "Deployment prerequisites are not ready"}</AlertTitle><AlertDescription><ul className="flex list-disc flex-col gap-2 pl-4">{requirementFailures.map((check, index) => <li key={`${check.kind}-${index}`}>{check.kind}: {check.detail || (check.missing || []).join(", ") || (zh ? "未满足" : "not ready")}</li>)}</ul></AlertDescription></Alert> : <Alert><CheckCircle2 /><AlertTitle>{zh ? "部署前置条件已满足" : "Deployment prerequisites are ready"}</AlertTitle></Alert>}
      </CardContent>
    </Card> : null}
    {health?.length ? <Card className="min-w-0">
      <CardHeader><CardTitle>{zh ? "交付健康" : "Delivery health"}</CardTitle></CardHeader>
      <CardContent><Table><TableHeader><TableRow><TableHead>{zh ? "目标" : "Target"}</TableHead><TableHead>{zh ? "状态" : "Status"}</TableHead></TableRow></TableHeader><TableBody>{health.map((item, index) => <TableRow key={`${item.target || item.kind || "health"}-${index}`}><TableCell className="whitespace-normal wrap-anywhere"><div className="flex flex-col gap-1"><span>{item.target || item.kind || (zh ? "服务" : "Service")}</span><span className="text-muted-foreground">{item.message || "-"}</span></div></TableCell><TableCell><Badge variant={["failed", "error", "unhealthy"].includes(item.status || "") ? "destructive" : "secondary"}>{item.status || "unknown"}</Badge></TableCell></TableRow>)}</TableBody></Table></CardContent>
    </Card> : null}
    {errors.length ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "校验错误" : "Validation errors"}</AlertTitle><AlertDescription><ul className="flex list-disc flex-col gap-2 pl-4">{errors.map((error, index) => <li key={`${index}-${error}`}>{error}</li>)}</ul></AlertDescription></Alert> : null}
    {steps.length ? <Card className="min-w-0"><CardHeader><CardTitle>{zh ? "部署步骤" : "Deploy steps"}</CardTitle></CardHeader><CardContent><StepLog steps={steps} lang={lang} /></CardContent></Card> : null}
  </aside>;
}
