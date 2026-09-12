import {
  AlertTriangle,
  Boxes,
  Info,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Bar, BarChart, CartesianGrid, XAxis } from "recharts";
import { CodeCell } from "../components/primitives";
import { Button, buttonVariants } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { cn } from "@/lib/utils";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Alert, AlertAction, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { formatTimestamp } from "../format";
import {
  fetchRegistryInventory,
  previewRegistryDeletion,
  purgeRegistryManifests,
  saveRegistryPolicy,
  type RegistryInventory,
  type RegistryManifest,
  type RegistryPolicy,
} from "../registryManagementApi";
import type { Lang } from "../types";
import { useRouter, toHref } from "../router";
import { findRegistryImage } from "../registryDetail";
import { PageHeader } from "./PageHeader";

const DEFAULT_POLICY: RegistryPolicy = {
  mode: "recommend",
  keepLast: 20,
  maxAgeDays: 30,
  systemKeepLast: 3,
  queueGraceHours: 0,
  gcGraceDays: 7,
  warningPercent: 75,
  criticalPercent: 85,
  emergencyPercent: 92,
};
const REGISTRY_PAGE_SIZE = 100;

function formatBytes(value?: number) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1000)), units.length - 1);
  return `${(bytes / 1000 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function keyFor(item: Pick<RegistryManifest, "repository" | "digest">) {
  return `${item.repository}@${item.digest}`;
}

function statusLabel(status: string | undefined, zh: boolean) {
  const labels: Record<string, [string, string]> = {
    protected: ["受保护", "Protected"],
    retained: ["策略保留", "Retained"],
    candidate: ["清理候选", "Candidate"],
    unknown: ["状态未知", "Unknown"],
  };
  const pair = labels[String(status || "unknown")] || labels.unknown;
  return zh ? pair[0] : pair[1];
}

export function RegistryPage({ lang, token }: { lang: Lang; token: string }) {
  const zh = lang === "zh";
  const { path, search, navigate } = useRouter();
  const routeSection = path.split("/")[2] || "inventory";
  const section = ["inventory", "policy", "image", "delete"].includes(routeSection) ? routeSection : "inventory";
  const showPolicy = section === "policy";
  const setShowPolicy = (value: boolean) => navigate(value ? "/registry/policy" : "/registry");
  const detailKey = new URLSearchParams(search).get("image") || "";
  const [detailState, setDetailState] = useState<{ key: string; status: "loading" | "ready" | "pending" | "missing" | "error"; image: RegistryManifest | null; error?: string }>({ key: "", status: "loading", image: null });
  const [detailRevision, setDetailRevision] = useState(0);
  const detail = detailState.key === detailKey ? detailState.image : null;
  const detailStatus = !detailKey ? "missing" : detailState.key === detailKey ? detailState.status : "loading";
  const [inventory, setInventory] = useState<RegistryInventory | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof previewRegistryDeletion>> | null>(null);
  const [policy, setPolicy] = useState<RegistryPolicy>(DEFAULT_POLICY);
  const [purgeElapsed, setPurgeElapsed] = useState(0);
  const inventoryRequest = useRef(0);

  useEffect(() => {
    if (busy !== "purge") return;
    const started = Date.now();
    setPurgeElapsed(0);
    const timer = window.setInterval(() => setPurgeElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [busy]);

  const load = useCallback(async (refresh = false, offset = 0, append = false) => {
    const request = ++inventoryRequest.current;
    setLoading(true);
    setError("");
    try {
      const next = await fetchRegistryInventory(token, refresh, undefined, {
        offset,
        limit: REGISTRY_PAGE_SIZE,
        query,
        status: filter,
      });
      if (request !== inventoryRequest.current) return;
      setInventory((current) => append && current ? {
        ...next,
        entries: [...(current.entries || []), ...(next.entries || [])],
      } : next);
      setPolicy(next.policy || DEFAULT_POLICY);
      if (!append) {
        setSelected((current) => {
          const available = new Set((next.entries || []).map(keyFor));
          return new Set([...current].filter((key) => available.has(key)));
        });
      }
    } catch (err) {
      if (request === inventoryRequest.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (request === inventoryRequest.current) setLoading(false);
    }
  }, [filter, query, token]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(false), query.trim() ? 180 : 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    if (!inventory?.scanPending) return;
    const timer = window.setTimeout(() => void load(false), 3000);
    return () => window.clearTimeout(timer);
  }, [inventory?.scanPending, load]);

  useEffect(() => {
    const onRefresh = () => { void load(false); setDetailRevision((value) => value + 1); };
    window.addEventListener("luma:refresh", onRefresh);
    return () => window.removeEventListener("luma:refresh", onRefresh);
  }, [load]);

  useEffect(() => {
    if (section !== "image" || !detailKey) return;
    const controller = new AbortController();
    setDetailState({ key: detailKey, status: "loading", image: null });
    void findRegistryImage(detailKey, (offset) => fetchRegistryInventory(token, false, controller.signal, {
      query: detailKey.split("@").pop(), limit: 100, offset,
    }), controller.signal).then((result) => {
      if (!controller.signal.aborted) setDetailState({ key: detailKey, ...result });
    }).catch((err) => {
      if (!controller.signal.aborted) setDetailState({ key: detailKey, status: "error", image: null, error: String(err instanceof Error ? err.message : err) });
    });
    return () => controller.abort();
  }, [detailKey, section, token, detailRevision]);

  useEffect(() => {
    if (section !== "image" || detailStatus !== "pending") return;
    const timer = window.setTimeout(() => setDetailRevision((value) => value + 1), 3000);
    return () => window.clearTimeout(timer);
  }, [section, detailStatus]);

  const entries = inventory?.entries || [];

  const selectable = entries;
  const allVisibleSelected = selectable.length > 0 && selectable.every((item) => selected.has(keyFor(item)));
  const summary = inventory?.summary || {};
  const usage = inventory?.usage || {};
  const hasUsage = !usage.error;
  const hasDiskPercent = hasUsage && usage.filesystemUsePercent != null;
  const diskPercent = Number(usage.filesystemUsePercent || 0);
  const unavailable = zh ? "暂不可用" : "Unavailable";
  const usageBytes = (value: number | undefined) => hasUsage && value != null ? formatBytes(value) : unavailable;
  const usageInterrupted = usage.error === "node agent restarted before task completion";
  const diskWarning = hasDiskPercent && diskPercent >= policy.warningPercent;
  const diskCritical = hasDiskPercent && diskPercent >= policy.criticalPercent;
  const diskEmergency = hasDiskPercent && diskPercent >= policy.emergencyPercent;
  const monthly = (usage.monthlyBlobs || []).slice(-6);

  const selectionItems = (inventory?.entries || []).filter((item) => selected.has(keyFor(item)));
  const deletionRepositories = new Map<string, RegistryManifest[]>();
  for (const item of preview?.selected || []) {
    const images = deletionRepositories.get(item.repository) || [];
    images.push(item);
    deletionRepositories.set(item.repository, images);
  }

  const toggle = (item: RegistryManifest) => {
    const key = keyFor(item);
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const openDeletePreview = async () => {
    if (!selectionItems.length) return;
    setBusy("preview");
    setError("");
    try {
      const result = await previewRegistryDeletion(token, selectionItems.map(({ repository, digest }) => ({ repository, digest })));
      setPreview(result);
      navigate("/registry/delete");
      window.scrollTo({ top: 0, behavior: "instant" });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  // One request does the whole job: stop Registry, delete the manifests,
  // garbage-collect their blobs, restart. Not recoverable by design.
  const purgeSelection = async () => {
    if (!preview?.allowed || !preview.selected?.length) return;
    setBusy("purge");
    setError("");
    try {
      const result = await purgeRegistryManifests(
        token,
        preview.selected.map(({ repository, digest }) => ({ repository, digest })),
      );
      const purged = new Set((result.purged || preview.selected).map(keyFor));
      setInventory((current) => current ? {
        ...current,
        entries: (current.entries || []).filter((item) => !purged.has(keyFor(item))),
      } : current);
      setPreview(null);
      navigate("/registry");
      window.scrollTo({ top: 0, behavior: "instant" });
      setSelected(new Set());
      setNotice(
        result.sharedLayersOnly
          ? (zh
            ? `已删除 ${preview.selected.length} 个镜像。共享层仍被其他镜像使用，未释放额外空间。`
            : `Deleted ${preview.selected.length} images. Shared layers are still in use, so no additional space was reclaimed.`)
          : (zh
            ? `已删除 ${preview.selected.length} 个镜像，释放 ${formatBytes(result.reclaimedBytes)}。`
            : `Deleted ${preview.selected.length} images; reclaimed ${formatBytes(result.reclaimedBytes)}.`),
      );
      // Deletion already updated the server snapshot. Read it without a full
      // rescan, and release the action immediately while that small request runs.
      void load(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const savePolicy = async () => {
    setBusy("policy");
    setError("");
    try {
      const result = await saveRegistryPolicy(token, policy);
      setPolicy(result.policy);
      setNotice(zh ? "保留策略已保存。" : "Retention policy saved.");
      setShowPolicy(false);
      await load(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const policyFields = [
    { key: "keepLast", label: zh ? "每仓库至少保留" : "Keep per repository", min: 1, max: 500 },
    { key: "maxAgeDays", label: zh ? "保留天数" : "Max age days", min: 1, max: 3650 },
    { key: "systemKeepLast", label: zh ? "系统版本保留" : "System versions", min: 1, max: 100 },
    { key: "queueGraceHours", label: zh ? "自动删除宽限期（小时）" : "Automatic deletion grace (hours)", min: 0, max: 720 },
    { key: "gcGraceDays", label: zh ? "自动回收恢复窗口（天）" : "Automatic reclamation grace (days)", min: 1, max: 365 },
    { key: "warningPercent", label: zh ? "容量预警（%）" : "Warning usage (%)", min: 1, max: 99 },
    { key: "criticalPercent", label: zh ? "容量严重（%）" : "Critical usage (%)", min: 2, max: 100 },
    { key: "emergencyPercent", label: zh ? "容量紧急（%）" : "Emergency usage (%)", min: 3, max: 100 },
  ] as const;
  const invalidPolicyField = (field: typeof policyFields[number]) => !Number.isInteger(policy[field.key]) || policy[field.key] < field.min || ("max" in field && policy[field.key] > field.max);
  const policyInvalid = policyFields.some(invalidPolicyField);
  const protectionBadge = (status?: string) => <Badge variant={status === "protected" ? "secondary" : "outline"}>{statusLabel(status, zh)}</Badge>;

  return (
    <div className="registry-workspace flex min-w-0 flex-col gap-6">
      <PageHeader
        meta={{
          eyebrow: zh ? "镜像生命周期" : "Image lifecycle",
          title: section === "delete" ? (zh ? "删除镜像" : "Delete images") : showPolicy ? (zh ? "镜像保留策略" : "Image retention policy") : (zh ? "Registry 镜像管理" : "Registry image management"),
          description: section === "delete"
            ? (zh ? "核对清理范围与影响后，确认执行。" : "Review the cleanup scope and impact before confirming.")
            : zh ? "查看镜像与引用情况，选择需要清理的镜像即可删除。" : "Review images and their references, then select images to delete.",
          metrics: section === "delete" ? (preview ? [
            { label: zh ? "选中镜像" : "Images", value: preview.selected?.length || 0 },
            { label: zh ? "标签" : "Tags", value: preview.selected?.reduce((count, item) => count + (item.tags?.length || 0), 0) || 0 },
            { label: zh ? "逻辑体积" : "Logical size", value: formatBytes(preview.logicalBytes) },
          ] : []) : [
            { label: zh ? "仓库" : "Repositories", value: inventory ? summary.repositoryCount || 0 : "—" },
            { label: zh ? "标签" : "Tags", value: inventory ? summary.tagCount || 0 : "—" },
            { label: zh ? "候选" : "Candidates", value: inventory ? summary.candidateCount || 0 : "—" },
          ],
          action: section === "delete" ? undefined : (
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="outline" type="button" disabled={loading || !!busy} onClick={() => { void load(true).then(() => setDetailRevision((value) => value + 1)); }}>
                {loading ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <RefreshCw data-icon="inline-start" />}
                {zh ? "重新扫描" : "Rescan"}
              </Button>
              <Button variant="outline" type="button" disabled={!!busy} onClick={() => setShowPolicy(!showPolicy)}>
                <Settings2 data-icon="inline-start" />{showPolicy ? (zh ? "返回镜像" : "Back to images") : (zh ? "保留策略" : "Retention")}
              </Button>
            </div>
          ),
        }}
      />

      <div className="flex min-w-0 flex-col gap-6">
        {inventory?.scanPending ? (
          <Alert>
            <Spinner aria-hidden="true" />
            <AlertTitle>{zh ? "首次镜像快照正在后台建立" : "Building the first image snapshot in the background"}</AlertTitle>
            <AlertDescription>{zh ? "可以离开本页；完成后再次进入会直接读取快照。" : "You may leave this page; future visits load the snapshot immediately."}</AlertDescription>
          </Alert>
        ) : inventory && !inventory.protectionComplete ? (
          <Alert variant="destructive">
            <AlertTriangle />
            <AlertTitle>{zh ? "引用扫描不完整，自动清理已暂停" : "Reference scan incomplete; automatic cleanup paused"}</AlertTitle>
            <AlertDescription>{inventory.referenceError || (zh ? "人工删除仍可继续，风险由操作者确认承担。" : "Manual deletion remains available after operator confirmation.")}</AlertDescription>
          </Alert>
        ) : null}
        {error ? <Alert variant="destructive"><AlertTriangle /><AlertTitle>{zh ? "操作失败" : "Operation failed"}</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}
        {usage.error ? (
          <Alert>
            <AlertTriangle />
            <AlertTitle>{zh ? "容量数据暂不可用" : "Storage data unavailable"}</AlertTitle>
            <AlertDescription>{usageInterrupted
              ? (zh ? "节点 agent 在容量采集完成前重启，本次扫描未取得容量数据。镜像清单仍可查看；节点恢复后可重新扫描。" : "The node agent restarted before storage inspection completed. Image inventory remains available; rescan after the node recovers.")
              : usage.error}</AlertDescription>
            <AlertAction><Button type="button" variant="outline" size="sm" disabled={loading || !!busy} onClick={() => void load(true)}>{zh ? "重新采集" : "Retry scan"}</Button></AlertAction>
          </Alert>
        ) : null}
        {notice ? (
          <Alert role="status">
            <ShieldCheck /><AlertTitle>{notice}</AlertTitle>
            <AlertAction><Button type="button" variant="ghost" size="sm" onClick={() => setNotice("")}>{zh ? "关闭" : "Dismiss"}</Button></AlertAction>
          </Alert>
        ) : null}

        {section === "inventory" ? <>
          <section className="grid min-w-0 gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label={zh ? "镜像仓库容量" : "Registry storage usage"}>
            <Card size="sm">
              <CardHeader><CardDescription>{zh ? "宿主磁盘" : "Host filesystem"}</CardDescription><CardTitle>{hasDiskPercent ? `${diskPercent}%` : unavailable}</CardTitle></CardHeader>
              <CardContent className="flex flex-col gap-3">
                <p className="text-sm text-muted-foreground">{zh ? `可用 ${usageBytes(usage.filesystemAvailableBytes)}` : `${usageBytes(usage.filesystemAvailableBytes)} available`}</p>
                {diskWarning ? <Badge variant={diskCritical || diskEmergency ? "destructive" : "warning"}>{diskEmergency ? (zh ? "容量紧急" : "Emergency capacity") : diskCritical ? (zh ? "容量严重" : "Critical capacity") : (zh ? "容量预警" : "Capacity warning")}</Badge> : null}
                {hasDiskPercent ? <Progress value={Math.max(0, Math.min(diskPercent, 100))} aria-label={zh ? "宿主磁盘使用率" : "Host filesystem usage"} /> : null}
              </CardContent>
            </Card>
            <Card size="sm">
              <CardHeader><CardDescription>{zh ? "镜像数据卷" : "Registry volume"}</CardDescription><CardTitle>{usageBytes(usage.volumeBytes)}</CardTitle></CardHeader>
              <CardContent><p className="break-all text-sm text-muted-foreground">{inventory?.registry?.volumeName || "—"}</p></CardContent>
            </Card>
            <Card size="sm">
              <CardHeader><CardDescription>{zh ? "受保护镜像清单" : "Protected manifests"}</CardDescription><CardTitle>{inventory ? summary.protectedCount || 0 : "—"}</CardTitle></CardHeader>
              <CardContent><p className="text-sm text-muted-foreground">{zh ? "运行、回滚、构建与系统引用" : "Runtime, rollback, build, and system references"}</p></CardContent>
            </Card>
            <Card size="sm">
              <CardHeader><CardDescription>{zh ? "最近扫描" : "Last scan"}</CardDescription><CardTitle>{summary.durationMs ? `${(summary.durationMs / 1000).toFixed(1)}s` : "—"}</CardTitle></CardHeader>
              <CardContent><p className="text-sm text-muted-foreground">{formatTimestamp(summary.scannedAt, lang)}</p></CardContent>
            </Card>
          </section>

          <Card>
            <CardHeader>
              <CardTitle>{zh ? "Registry 存储月度分布" : "Registry storage by month"}</CardTitle>
              <CardDescription>{zh ? "现存 Blob 按文件最后修改月份汇总，展示最近 6 个有数据的月份；不是每月总容量快照。" : "Existing blobs grouped by file modification month, showing the latest six populated months; not historical capacity snapshots."}</CardDescription>
            </CardHeader>
            <CardContent>
              {loading && !inventory ? <Skeleton className="h-48 w-full" aria-label={zh ? "正在加载月度分布" : "Loading monthly distribution"} /> : hasUsage && monthly.length ? <>
                <ChartContainer config={{ bytes: { label: zh ? "存储占用" : "Storage usage", color: "var(--chart-1)" } }} className="h-48 w-full">
                  <BarChart accessibilityLayer data={monthly}>
                    <CartesianGrid vertical={false} />
                    <XAxis dataKey="month" tickLine={false} axisLine={false} tickMargin={8} />
                    <ChartTooltip content={<ChartTooltipContent formatter={(value) => formatBytes(Number(value))} />} />
                    <Bar dataKey="bytes" fill="var(--color-bytes)" radius={4} maxBarSize={72} isAnimationActive={false} />
                  </BarChart>
                </ChartContainer>
                <div className="sr-only"><Table><TableHeader><TableRow><TableHead>{zh ? "月份" : "Month"}</TableHead><TableHead>{zh ? "存储占用" : "Storage usage"}</TableHead></TableRow></TableHeader><TableBody>{monthly.map((item) => <TableRow key={item.month}><TableCell>{item.month}</TableCell><TableCell>{formatBytes(item.bytes)}</TableCell></TableRow>)}</TableBody></Table></div>
              </> : <Empty><EmptyHeader><EmptyMedia variant="icon"><Boxes /></EmptyMedia><EmptyTitle>{zh ? "暂无月度分布" : "No monthly distribution"}</EmptyTitle><EmptyDescription>{usage.error
                ? (zh ? "容量采集失败，暂无法显示月度分布。请重新采集后查看。" : "Storage collection failed. Retry the scan to load monthly data.")
                : (zh ? "暂无月度数据，重新扫描后显示存储分布。" : "No monthly data yet. Rescan to load the storage distribution.")}</EmptyDescription></EmptyHeader></Empty>}
            </CardContent>
          </Card>

          <Card aria-busy={loading}>
            <CardHeader><CardTitle>{zh ? "镜像列表" : "Images"}</CardTitle><CardDescription>{zh ? "按仓库、标签或摘要搜索，并选择需要删除的镜像。" : "Search by repository, tag, or digest, then select images to delete."}</CardDescription></CardHeader>
            <CardContent className="flex min-w-0 flex-col gap-4">
              <FieldGroup>
                <Field>
                  <FieldLabel htmlFor="registry-search" className="sr-only">{zh ? "搜索镜像" : "Search images"}</FieldLabel>
                  <InputGroup className="max-w-xl"><InputGroupAddon><Search /></InputGroupAddon><InputGroupInput id="registry-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={zh ? "搜索仓库、标签或摘要" : "Search repository, tag, or digest"} /></InputGroup>
                </Field>
                <Field>
                  <FieldLabel id="registry-filter-label" className="sr-only">{zh ? "镜像保护状态" : "Image protection status"}</FieldLabel>
                  <ToggleGroup className="flex-wrap" variant="outline" size="sm" value={[filter]} onValueChange={(values) => { if (values[0]) setFilter(String(values[0])); }} aria-labelledby="registry-filter-label">
                    {["all", "protected", "retained", "candidate", "unknown"].map((value) => <ToggleGroupItem key={value} value={value}>{value === "all" ? (zh ? "全部" : "All") : statusLabel(value, zh)}</ToggleGroupItem>)}
                  </ToggleGroup>
                </Field>
              </FieldGroup>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-sm text-muted-foreground" role="status">{zh ? `已选择 ${selected.size} 个镜像` : `${selected.size} images selected`}</p>
                <Button variant="destructive" type="button" disabled={!selected.size || !!busy} onClick={() => void openDeletePreview()}>{busy === "preview" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <Trash2 data-icon="inline-start" />}{busy === "preview" ? (zh ? "正在分析…" : "Reviewing…") : (zh ? `删除镜像${selected.size ? `（${selected.size}）` : ""}` : `Delete images${selected.size ? ` (${selected.size})` : ""}`)}</Button>
              </div>
              <Table containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "镜像列表，可横向滚动" : "Image inventory, horizontally scrollable" }} className="min-w-[960px]">
                  <TableHeader><TableRow>
                    <TableHead className="w-10"><Checkbox checked={allVisibleSelected} indeterminate={!allVisibleSelected && selectable.some((item) => selected.has(keyFor(item)))} disabled={!selectable.length || !!busy} aria-label={zh ? "选择当前页所有镜像" : "Select all images on this page"} onCheckedChange={(checked) => setSelected((current) => { const next = new Set(current); selectable.forEach((item) => checked ? next.add(keyFor(item)) : next.delete(keyFor(item))); return next; })} /></TableHead>
                    <TableHead>{zh ? "仓库 / 摘要" : "Repository / digest"}</TableHead><TableHead>{zh ? "标签" : "Tags"}</TableHead><TableHead>{zh ? "平台" : "Platforms"}</TableHead><TableHead>{zh ? "体积" : "Size"}</TableHead><TableHead>{zh ? "创建时间" : "Created"}</TableHead><TableHead>{zh ? "保护状态" : "Protection"}</TableHead>
                  </TableRow></TableHeader>
                  <TableBody>
                    {entries.map((item) => <TableRow key={keyFor(item)} data-state={selected.has(keyFor(item)) ? "selected" : undefined}>
                      <TableCell><Checkbox checked={selected.has(keyFor(item))} disabled={!!busy} onCheckedChange={() => toggle(item)} aria-label={`${item.repository} ${item.digest}`} /></TableCell>
                      <TableCell><span className="flex w-80 flex-col items-start gap-1 whitespace-normal"><a className={cn(buttonVariants({ variant: "link", size: "sm" }), "h-auto max-w-full whitespace-normal break-all px-0 text-left")} href={toHref(`/registry/image?image=${encodeURIComponent(keyFor(item))}`)} onClick={(event) => { if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); navigate(`/registry/image?image=${encodeURIComponent(keyFor(item))}`); }}>{item.repository}</a><CodeCell value={item.digest} /></span></TableCell>
                      <TableCell><span className="flex max-w-52 flex-wrap gap-1">{item.tags?.length ? item.tags.slice(0, 4).map((tag) => <Badge key={tag} variant="secondary" title={tag}><span className="max-w-44 truncate">{tag}</span></Badge>) : <span className="text-muted-foreground">—</span>}{(item.tags || []).length > 4 ? <Badge variant="outline">+{item.tags.length - 4}</Badge> : null}</span></TableCell>
                      <TableCell><span className="flex flex-col items-start gap-1">{item.platforms?.length ? item.platforms.map((platform) => <Badge key={platform} variant="outline">{platform}</Badge>) : <span className="text-muted-foreground">—</span>}</span></TableCell>
                      <TableCell>{formatBytes(item.logicalBytes)}</TableCell><TableCell>{formatTimestamp(item.createdAt || item.lastModified, lang)}</TableCell>
                      <TableCell><span className="flex max-w-64 flex-col items-start gap-1">{protectionBadge(item.protectionStatus)}{item.protectionReasons?.slice(0, 2).map((reason, index) => <span className="max-w-full truncate text-xs text-muted-foreground" title={reason.source || reason.kind} key={`${reason.kind}-${index}`}>{reason.source || reason.kind}</span>)}</span></TableCell>
                    </TableRow>)}
                    {!entries.length ? <TableRow><TableCell colSpan={7}>{loading ? <div className="flex flex-col gap-3 py-4" role="status" aria-label={zh ? "正在加载镜像" : "Loading images"}><Skeleton className="h-5 w-2/3" /><Skeleton className="h-5 w-1/2" /><Skeleton className="h-5 w-3/4" /></div> : <Empty><EmptyHeader><EmptyMedia variant="icon"><Boxes /></EmptyMedia><EmptyTitle>{zh ? "没有匹配的镜像" : "No matching images"}</EmptyTitle><EmptyDescription>{zh ? "调整搜索或保护状态后重试。" : "Try another search or protection status."}</EmptyDescription></EmptyHeader></Empty>}</TableCell></TableRow> : null}
                  </TableBody>
                </Table>
            </CardContent>
            {inventory?.page?.hasMore ? <CardFooter className="justify-center"><Button variant="outline" type="button" disabled={loading || !!busy} onClick={() => void load(false, (inventory.page?.offset || 0) + (inventory.page?.limit || REGISTRY_PAGE_SIZE), true)}>{loading ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}{loading ? (zh ? "加载中…" : "Loading…") : (zh ? `加载更多（已显示 ${entries.length} / ${inventory.page.total || 0}）` : `Load more (${entries.length} / ${inventory.page.total || 0})`)}</Button></CardFooter> : null}
          </Card>
        </> : null}

        {showPolicy ? <Card>
          <CardHeader><CardTitle>{zh ? "保留与安全窗口" : "Retention and safety windows"}</CardTitle><CardDescription>{zh ? "此策略只控制自动清理；手动删除镜像会立即删除并回收空间。" : "This policy controls automatic cleanup. Manual image deletion removes images and reclaims space immediately."}</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-4">
            <form id="registry-policy-form" onSubmit={(event) => { event.preventDefault(); if (!policyInvalid) void savePolicy(); }}>
              <FieldGroup className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                <Field data-disabled={!!busy}><FieldLabel id="registry-policy-mode-label">{zh ? "模式" : "Mode"}</FieldLabel><ToggleGroup className="flex-wrap" variant="outline" value={[policy.mode]} disabled={!!busy} aria-labelledby="registry-policy-mode-label" onValueChange={(values) => { if (values[0]) setPolicy({ ...policy, mode: values[0] as RegistryPolicy["mode"] }); }}>{[{ value: "off", label: zh ? "关闭" : "Off" }, { value: "recommend", label: zh ? "仅建议" : "Recommend" }, { value: "enforce", label: zh ? "自动执行" : "Enforce" }].map((item) => <ToggleGroupItem key={item.value} value={item.value}>{item.label}</ToggleGroupItem>)}</ToggleGroup></Field>
                {policyFields.map((field) => <Field key={field.key} data-disabled={!!busy} data-invalid={invalidPolicyField(field)}><FieldLabel htmlFor={`registry-policy-${field.key}`}>{field.label}</FieldLabel><Input id={`registry-policy-${field.key}`} type="number" required disabled={!!busy} min={field.min} max={"max" in field ? field.max : undefined} step={1} value={policy[field.key]} aria-invalid={invalidPolicyField(field)} aria-describedby={invalidPolicyField(field) ? `registry-policy-${field.key}-error` : undefined} onChange={(event) => setPolicy({ ...policy, [field.key]: Number(event.target.value) })} />{invalidPolicyField(field) ? <FieldError id={`registry-policy-${field.key}-error`}>{zh ? `请输入不小于 ${field.min}${"max" in field ? ` 且不大于 ${field.max}` : ""} 的整数。` : `Enter an integer of at least ${field.min}${"max" in field ? ` and at most ${field.max}` : ""}.`}</FieldError> : field.key === "queueGraceHours" ? <FieldDescription>{zh ? "0 表示自动删除立即执行。" : "Use 0 to run automatic deletion immediately."}</FieldDescription> : null}</Field>)}
              </FieldGroup>
            </form>
            {policy.mode === "enforce" ? <Alert><AlertTriangle /><AlertTitle>{zh ? "自动清理已启用" : "Automatic cleanup enabled"}</AlertTitle><AlertDescription>{policy.queueGraceHours > 0 ? (zh ? "候选镜像将在宽限期后自动删除，恢复窗口结束后自动回收空间。" : "Candidate images are automatically deleted after the grace period; space is reclaimed after the recovery window.") : (zh ? "候选镜像将立即自动删除，恢复窗口结束后自动回收空间。" : "Candidate images are automatically deleted immediately; space is reclaimed after the recovery window.")}</AlertDescription></Alert> : null}
          </CardContent>
          <CardFooter className="flex-wrap justify-end gap-2"><Button type="button" variant="outline" disabled={!!busy} onClick={() => setShowPolicy(false)}>{zh ? "取消" : "Cancel"}</Button><Button type="submit" form="registry-policy-form" disabled={!!busy || policyInvalid}>{busy === "policy" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}{busy === "policy" ? (zh ? "保存中…" : "Saving…") : (zh ? "保存策略" : "Save policy")}</Button></CardFooter>
        </Card> : null}

        {section === "image" ? <Card>
          <CardHeader><CardTitle><span className="break-all">{detail?.repository || (zh ? "镜像详情" : "Image details")}</span></CardTitle>{detail ? <CardDescription><CodeCell value={detail.digest} /></CardDescription> : null}</CardHeader>
          <CardContent className="flex min-w-0 flex-col gap-6">
            {detail ? <>
              <dl className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                <div className="flex min-w-0 flex-col gap-2"><dt className="text-sm text-muted-foreground">{zh ? "标签" : "Tags"}</dt><dd className="flex flex-wrap gap-1">{detail.tags?.length ? detail.tags.map((tag) => <Badge key={tag} variant="secondary" className="h-auto max-w-full"><span className="whitespace-normal break-all">{tag}</span></Badge>) : "—"}</dd></div>
                <div className="flex min-w-0 flex-col gap-2"><dt className="text-sm text-muted-foreground">{zh ? "平台" : "Platforms"}</dt><dd className="flex flex-wrap gap-1">{detail.platforms?.length ? detail.platforms.map((platform) => <Badge key={platform} variant="outline">{platform}</Badge>) : "—"}</dd></div>
                <div className="flex flex-col gap-2"><dt className="text-sm text-muted-foreground">{zh ? "逻辑大小" : "Logical size"}</dt><dd>{formatBytes(detail.logicalBytes)}</dd></div>
                <div className="flex flex-col gap-2"><dt className="text-sm text-muted-foreground">{zh ? "创建时间" : "Created"}</dt><dd>{formatTimestamp(detail.createdAt || detail.lastModified, lang)}</dd></div>
                <div className="flex flex-col gap-2"><dt className="text-sm text-muted-foreground">{zh ? "保护状态" : "Protection"}</dt><dd>{protectionBadge(detail.protectionStatus)}</dd></div>
                <div className="flex min-w-0 flex-col gap-2"><dt className="text-sm text-muted-foreground">{zh ? "媒体类型" : "Media type"}</dt><dd className="break-all">{detail.mediaType || "—"}</dd></div>
              </dl>
              <Card size="sm"><CardHeader><CardTitle>{zh ? "引用关系" : "References"}</CardTitle></CardHeader><CardContent>{detail.protectionReasons?.length ? <Table containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "镜像引用，可横向滚动" : "Image references, horizontally scrollable" }}><TableHeader><TableRow><TableHead>{zh ? "类型" : "Kind"}</TableHead><TableHead>{zh ? "来源" : "Source"}</TableHead><TableHead>{zh ? "引用" : "Reference"}</TableHead></TableRow></TableHeader><TableBody>{detail.protectionReasons.map((reason, index) => <TableRow key={index}><TableCell><Badge variant="outline">{reason.kind}</Badge></TableCell><TableCell>{reason.source || "—"}</TableCell><TableCell><CodeCell value={reason.reference || "—"} /></TableCell></TableRow>)}</TableBody></Table> : <Empty><EmptyHeader><EmptyTitle>{zh ? "没有已知引用" : "No known references"}</EmptyTitle></EmptyHeader></Empty>}</CardContent></Card>
              <Card size="sm"><CardHeader><CardTitle>{zh ? "平台镜像清单" : "Platform manifests"}</CardTitle></CardHeader><CardContent>{detail.childManifestDigests?.length ? <Table containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "平台镜像清单，可横向滚动" : "Platform manifests, horizontally scrollable" }}><TableHeader><TableRow><TableHead>{zh ? "镜像摘要" : "Digest"}</TableHead></TableRow></TableHeader><TableBody>{detail.childManifestDigests.map((digest) => <TableRow key={digest}><TableCell><CodeCell value={digest} /></TableCell></TableRow>)}</TableBody></Table> : <Empty><EmptyHeader><EmptyTitle>{zh ? "没有独立平台清单" : "No separate platform manifests"}</EmptyTitle></EmptyHeader></Empty>}</CardContent></Card>
            </> : detailStatus === "loading" || detailStatus === "pending" ? <div className="flex flex-col gap-4" role="status" aria-busy="true"><p className="text-sm text-muted-foreground">{detailStatus === "pending" ? (zh ? "镜像索引正在建立，完成后将自动刷新。" : "Building image index; this page will refresh when ready.") : (zh ? "正在查询镜像…" : "Loading image…")}</p><Skeleton className="h-5 w-3/4" /><Skeleton className="h-5 w-1/2" /><Skeleton className="h-24 w-full" /></div> : detailStatus === "error" ? <Alert variant="destructive"><AlertTriangle /><AlertTitle>{zh ? "镜像查询失败" : "Image lookup failed"}</AlertTitle><AlertDescription>{detailState.error}</AlertDescription><AlertAction><Button variant="outline" type="button" size="sm" onClick={() => setDetailRevision((value) => value + 1)}>{zh ? "重新查询" : "Retry lookup"}</Button></AlertAction></Alert> : <Empty><EmptyHeader><EmptyMedia variant="icon"><Boxes /></EmptyMedia><EmptyTitle>{zh ? "未找到此镜像" : "Image not found"}</EmptyTitle><EmptyDescription>{zh ? "它可能已经删除或不在当前索引中。" : "It may have been deleted or is absent from the current index."}</EmptyDescription></EmptyHeader><EmptyContent><Button variant="outline" type="button" onClick={() => setDetailRevision((value) => value + 1)}>{zh ? "重新查询" : "Retry lookup"}</Button></EmptyContent></Empty>}
          </CardContent>
          <CardFooter><Button variant="outline" type="button" onClick={() => navigate("/registry")}>{zh ? "返回镜像" : "Back to images"}</Button></CardFooter>
        </Card> : null}

        {section === "delete" && !preview ? <Card><CardHeader><CardTitle>{zh ? "重新选择清理范围" : "Select cleanup scope"}</CardTitle></CardHeader><CardContent><Empty><EmptyHeader><EmptyMedia variant="icon"><Boxes /></EmptyMedia><EmptyTitle>{zh ? "当前没有待删除镜像" : "No images selected for deletion"}</EmptyTitle><EmptyDescription>{zh ? "为确保清理范围准确，刷新页面后需要重新选择镜像并分析。" : "After refreshing, select images and preview again to confirm the exact cleanup scope."}</EmptyDescription></EmptyHeader><EmptyContent><Button type="button" onClick={() => navigate("/registry")}>{zh ? "返回镜像列表" : "Back to images"}</Button></EmptyContent></Empty></CardContent></Card> : null}
        {section === "delete" && preview ? <Card className="registry-delete-page" aria-labelledby="registry-delete-title" aria-busy={busy === "purge"}>
          <CardHeader><CardTitle id="registry-delete-title">{zh ? "清理范围" : "Cleanup scope"}</CardTitle><CardDescription>{zh ? `将删除以下 ${preview.selected?.length || 0} 个镜像及其全部标签，并清理不再使用的镜像数据。` : `Delete the ${preview.selected?.length || 0} images below and all their tags, then remove their unused image data.`}</CardDescription></CardHeader>
          <CardContent className="flex min-w-0 flex-col gap-4">
            <Table containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "待删除镜像，可横向滚动" : "Selected images, horizontally scrollable" }} className="min-w-[560px] table-fixed"><TableHeader><TableRow><TableHead className="w-1/4">{zh ? "标签" : "Tags"}</TableHead><TableHead>{zh ? "镜像摘要" : "Digest"}</TableHead><TableHead className="w-28 text-right">{zh ? "逻辑体积" : "Logical size"}</TableHead></TableRow></TableHeader>
                {[...deletionRepositories].map(([repository, images]) => <TableBody key={repository}>
                  <TableRow><TableHead colSpan={3} scope="rowgroup"><span className="flex items-center gap-2"><span className="truncate" title={repository}>{repository}</span><Badge variant="secondary">{images.length}</Badge></span></TableHead></TableRow>
                  {images.map((item) => <TableRow key={keyFor(item)}><TableCell><span className="flex flex-wrap gap-1">{item.tags?.length ? item.tags.map((tag) => <Badge key={tag} variant="outline" className="h-auto max-w-full"><span className="whitespace-normal break-all">{tag}</span></Badge>) : <span className="text-muted-foreground">{zh ? "无标签" : "Untagged"}</span>}</span></TableCell><TableCell><code className="block whitespace-normal break-all font-mono text-xs">{item.digest}</code></TableCell><TableCell className="text-right">{formatBytes(item.logicalBytes)}</TableCell></TableRow>)}
                </TableBody>)}
              </Table>
            <Alert><Info /><AlertTitle>{zh ? "释放空间以清理结果为准" : "Freed space is measured after cleanup"}</AlertTitle><AlertDescription>{zh ? "其他镜像仍在使用的共享层会保留。清理完成后将显示实际释放空间。" : "Layers still used by other images are retained. Actual reclaimed space is shown when cleanup completes."}</AlertDescription></Alert>
            <Alert variant="destructive"><AlertTriangle /><AlertTitle>{zh ? "删除后无法恢复" : "Deletion is permanent"}</AlertTitle><AlertDescription>{zh ? "此操作不创建备份。删除与回收期间 Registry 暂停服务，无法推送或拉取镜像。" : "No backup is created. The registry is unavailable for pushes and pulls during deletion and reclamation."}</AlertDescription></Alert>
            {preview.risks?.length ? <Alert variant="destructive"><AlertTriangle /><AlertTitle>{zh ? "存在引用风险" : "Referenced images are included"}</AlertTitle><AlertDescription>{zh ? "部分镜像仍有服务引用，或引用扫描不完整。删除后，相关服务重启或回滚时可能无法拉取镜像。" : "Some images remain referenced, or reference scanning is incomplete. Affected services may fail to pull their image on restart or rollback."}</AlertDescription></Alert> : null}
            {preview.blocked?.length ? <Alert variant="destructive"><AlertTriangle /><AlertTitle>{zh ? "当前范围无法执行" : "This selection cannot be processed"}</AlertTitle><AlertDescription>{zh ? `${preview.blocked.length} 项因镜像不存在或批量范围过大而无法处理，请返回调整。` : `${preview.blocked.length} items are missing or exceed the batch limit. Go back to adjust the selection.`}</AlertDescription></Alert> : null}
            {busy === "purge" ? <Alert role="status"><Spinner aria-hidden="true" /><AlertTitle>{zh ? "正在删除镜像" : "Deleting images"}</AlertTitle><AlertDescription>{zh ? `已等待 ${purgeElapsed} 秒。正在删除镜像并清理空间，完成后会自动返回列表。` : `Elapsed: ${purgeElapsed}s. Deleting images and freeing space; you will return to the list when complete.`}</AlertDescription></Alert> : null}
          </CardContent>
          <CardFooter className="flex-wrap justify-end gap-2"><Button variant="outline" type="button" disabled={!!busy} onClick={() => { setPreview(null); navigate("/registry"); }}>{zh ? "取消" : "Cancel"}</Button><Button variant="destructive" type="button" disabled={!preview.allowed || !!busy} onClick={() => void purgeSelection()}>{busy === "purge" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <Trash2 data-icon="inline-start" />}{busy === "purge" ? (zh ? "正在删除…" : "Deleting…") : (zh ? "确认删除" : "Delete images")}</Button></CardFooter>
        </Card> : null}
      </div>
    </div>
  );
}
