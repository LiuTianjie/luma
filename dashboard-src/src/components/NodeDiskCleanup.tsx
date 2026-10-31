import { useState } from "react";
import { CircleAlert, HardDrive, ScanSearch, Trash2 } from "@/components/icons";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useConfirm } from "./ConfirmDialog";
import { runNodeDiskCleanup } from "../nodeMaintenanceApi";
import { formatBytes, formatDateTime } from "../format";
import type { DashboardNode, Lang, NodeDiskCleanupResult } from "../types";

export const DISK_HYGIENE_CAPABILITY = "node-disk-hygiene-v1";

const KEEP_LABELS: Record<string, [string, string]> = {
  container: ["used by a container", "被容器使用"],
  nomad: ["referenced by a Nomad allocation", "被 Nomad 分配引用"],
  control: ["Luma Control image", "Luma Control 镜像"],
  protected: ["protected", "受保护"],
  recent: ["newer than the age limit", "未到清理年限"],
  "unknown-age": ["age unknown", "无法确定时间"],
};

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="m-0 min-w-0 text-sm wrap-break-word">{value || "-"}</dd>
    </div>
  );
}

function keptSummary(kept: Record<string, number> | undefined, zh: boolean): string {
  const parts = Object.entries(kept || {})
    .filter(([, count]) => count > 0)
    .map(([reason, count]) => `${count} ${KEEP_LABELS[reason]?.[zh ? 1 : 0] ?? reason}`);
  return parts.join(zh ? "，" : ", ");
}

export function NodeDiskCleanup({ lang, token, node }: { lang: Lang; token: string; node: DashboardNode }) {
  const zh = lang === "zh";
  const { confirm, element: confirmElement } = useConfirm(lang);
  const [busy, setBusy] = useState<"" | "preview" | "apply">("");
  const [preview, setPreview] = useState<NodeDiskCleanupResult | null>(null);
  const [applied, setApplied] = useState<NodeDiskCleanupResult | null>(null);
  const [error, setError] = useState("");
  const hygiene = node.diskHygiene;
  const supported = (node.storageCapabilities || []).includes(DISK_HYGIENE_CAPABILITY);
  const ready = supported && node.agentStatus === "ready";
  const nodeName = node.name || "";

  const run = async (dryRun: boolean) => {
    setBusy(dryRun ? "preview" : "apply");
    setError("");
    try {
      const result = await runNodeDiskCleanup(token, nodeName, dryRun);
      if (dryRun) {
        setPreview(result);
        setApplied(null);
      } else {
        setApplied(result);
        setPreview(null);
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy("");
    }
  };

  const apply = async () => {
    const count = preview?.images?.candidateCount ?? 0;
    const ok = await confirm({
      title: zh ? `清理 ${nodeName} 的磁盘` : `Clean up ${nodeName}`,
      body: zh
        ? `将删除 ${count} 个未使用的镜像，并清理超过 ${preview?.minAgeDays ?? 7} 天未使用的构建缓存。删除前会重新检查容器和 Nomad 分配，正在使用的镜像不会被删除。`
        : `This removes ${count} unused image(s) and build cache unused for ${preview?.minAgeDays ?? 7} days. Containers and Nomad allocations are checked again first; images in use are never removed.`,
      confirmLabel: zh ? "开始清理" : "Clean up",
      warning: zh ? "被删除的镜像需要重新拉取或构建才能再次使用。" : "Removed images must be pulled or rebuilt before they can be used again.",
    });
    if (ok) await run(false);
  };

  const images = preview?.images;
  const candidates = images?.candidates || [];
  const description = !supported
    ? (zh ? "此节点的 agent 版本尚不支持磁盘清理。升级节点后会自动启用。" : "This node agent does not support disk cleanup yet. Update the node to enable it.")
    : hygiene?.enabled === false
      ? (zh ? "此节点已关闭自动清理，仍可手动清理。" : "Automatic cleanup is turned off on this node; you can still run it manually.")
      : zh
        ? `每 ${hygiene?.intervalHours ?? 24} 小时自动删除超过 ${hygiene?.minAgeDays ?? 7} 天且没有容器或 Nomad 分配使用的镜像，以及同样久未使用的构建缓存。`
        : `Every ${hygiene?.intervalHours ?? 24} hours, removes images no container or Nomad allocation uses that are older than ${hygiene?.minAgeDays ?? 7} days, plus build cache unused for as long.`;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{zh ? "磁盘清理" : "Disk cleanup"}</CardTitle>
        <CardDescription>{description}</CardDescription>
        <CardAction className="flex flex-wrap items-center justify-end gap-2">
          <Button variant="outline" size="sm" disabled={!ready || Boolean(busy)} onClick={() => void run(true)}>
            {busy === "preview" ? <Spinner data-icon="inline-start" aria-hidden="true" /> : <ScanSearch data-icon="inline-start" />}
            {zh ? "预览清理" : "Preview cleanup"}
          </Button>
          {preview && (images?.candidateCount ?? 0) > 0 ? (
            <Button variant="destructive" size="sm" disabled={!ready || Boolean(busy)} onClick={() => void apply()}>
              {busy === "apply" ? <Spinner data-icon="inline-start" aria-hidden="true" /> : <Trash2 data-icon="inline-start" />}
              {zh ? `删除 ${images?.candidateCount} 个镜像` : `Remove ${images?.candidateCount} image(s)`}
            </Button>
          ) : null}
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {supported ? (
          <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 md:grid-cols-4">
            <Fact
              label={zh ? "上次清理" : "Last cleanup"}
              value={hygiene?.lastRunAt
                ? `${formatDateTime(hygiene.lastRunAt, lang)}${hygiene.lastTrigger === "manual" ? (zh ? "（手动）" : " (manual)") : ""}`
                : (zh ? "尚未运行" : "Not run yet")}
            />
            <Fact label={zh ? "释放空间" : "Reclaimed"} value={hygiene?.lastRunAt ? formatBytes(hygiene.lastReclaimedBytes ?? 0) : "-"} />
            <Fact label={zh ? "结果" : "Result"} value={hygiene?.lastMessage || "-"} />
            <Fact
              label={zh ? "下次自动清理" : "Next automatic run"}
              value={hygiene?.enabled === false ? (zh ? "已关闭" : "Off") : hygiene?.nextRunAt ? formatDateTime(hygiene.nextRunAt, lang) : (zh ? "agent 启动后约 10 分钟" : "About 10 minutes after the agent starts")}
            />
          </dl>
        ) : null}

        {error ? (
          <Alert variant="destructive">
            <CircleAlert />
            <AlertTitle>{zh ? "清理请求失败" : "Cleanup request failed"}</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : null}

        {applied ? (
          <Alert variant={applied.status === "failed" ? "destructive" : "default"}>
            <HardDrive />
            <AlertTitle>{zh ? "清理完成" : "Cleanup finished"}</AlertTitle>
            <AlertDescription>{applied.message}</AlertDescription>
          </Alert>
        ) : null}

        {preview ? (
          <div className="flex flex-col gap-3">
            <Alert variant={images?.status === "ok" ? "default" : "destructive"}>
              <ScanSearch />
              <AlertTitle>{zh ? "预览结果（未删除任何内容）" : "Preview (nothing was removed)"}</AlertTitle>
              <AlertDescription>
                {preview.message}
                {keptSummary(images?.kept, zh) ? <><br />{zh ? "保留：" : "Kept: "}{keptSummary(images?.kept, zh)}</> : null}
              </AlertDescription>
            </Alert>
            {candidates.length ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{zh ? "镜像" : "Image"}</TableHead>
                    <TableHead>{zh ? "距今（天）" : "Age (days)"}</TableHead>
                    <TableHead>{zh ? "大小" : "Size"}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {candidates.map((item) => {
                    const label = item.refs?.[0] || item.id.slice(0, 19);
                    return (
                      <TableRow key={item.id}>
                        <TableCell className="max-w-96 truncate font-mono text-xs" title={[...(item.refs || []), item.id].join("\n")}>{label}</TableCell>
                        <TableCell className="tabular-nums">{typeof item.ageDays === "number" ? item.ageDays : "-"}</TableCell>
                        <TableCell className="tabular-nums">{formatBytes(item.sizeBytes ?? 0)}</TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            ) : null}
          </div>
        ) : null}
        {confirmElement}
      </CardContent>
    </Card>
  );
}
