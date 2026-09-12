import "./resourceWorkspaces.css";
import {
  AlertTriangle,
  Boxes,
  Clock3,
  Database,
  HardDrive,
  Info,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { CodeCell, SelectControl, StatePill } from "../components/primitives";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Alert, AlertAction, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Field, FieldLabel } from "@/components/ui/field";
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
  const section = routeSection === "cleanup" ? "inventory" : routeSection;
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
  const diskTone = diskPercent >= (policy.emergencyPercent || 92) ? "critical" : diskPercent >= (policy.criticalPercent || 85) ? "warning" : "healthy";
  const monthly = (usage.monthlyBlobs || []).slice(-6);
  const monthlyMax = Math.max(...monthly.map((item) => Number(item.bytes || 0)), 1);

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

  return (
    <div className="registry-workspace">
      <PageHeader
        meta={{
          eyebrow: zh ? "镜像生命周期" : "Image lifecycle",
          title: section === "delete" ? (zh ? "删除镜像" : "Delete images") : (zh ? "Registry 镜像管理" : "Registry image management"),
          description: section === "delete"
            ? (zh ? "核对清理范围与影响后，确认执行。" : "Review the cleanup scope and impact before confirming.")
            : zh
            ? "查看镜像与引用情况，选择需要清理的镜像即可删除。"
            : "Review images and their references, then select images to delete.",
          metrics: section === "delete" ? (preview ? [
            { label: zh ? "选中镜像" : "Images", value: preview.selected?.length || 0 },
            { label: zh ? "标签" : "Tags", value: preview.selected?.reduce((count, item) => count + (item.tags?.length || 0), 0) || 0 },
            { label: zh ? "逻辑体积" : "Logical size", value: formatBytes(preview.logicalBytes) },
          ] : []) : [
            { label: zh ? "仓库" : "Repositories", value: summary.repositoryCount || 0 },
            { label: "Tags", value: summary.tagCount || 0 },
            { label: zh ? "候选" : "Candidates", value: summary.candidateCount || 0 },
          ],
          action: section === "delete" ? undefined : (
            <div className="registry-header-actions">
              <Button variant="outline" type="button" disabled={loading || !!busy} onClick={() => { void load(true).then(() => setDetailRevision((value) => value + 1)); }}>
                <RefreshCw size={15} className={loading ? "spin" : ""} /> {zh ? "重新扫描" : "Rescan"}
              </Button>
              <Button variant="outline" type="button" onClick={() => setShowPolicy(!showPolicy)}>
                <Settings2 size={15} /> {zh ? "保留策略" : "Retention"}
              </Button>
            </div>
          ),
        }}
      />

      <main className="registry-page">
        {inventory?.scanPending ? (
          <Alert>
            <RefreshCw className="animate-spin" />
            <AlertTitle>{zh ? "首次镜像快照正在后台建立" : "Building the first image snapshot in the background"}</AlertTitle>
            <AlertDescription>{zh ? "可以离开本页；完成后再次进入会直接读取快照。" : "You may leave this page; future visits load the snapshot immediately."}</AlertDescription>
          </Alert>
        ) : !inventory?.protectionComplete ? (
          <Alert variant="destructive">
            <AlertTriangle />
            <AlertTitle>{zh ? "引用扫描不完整，自动清理已暂停" : "Reference scan incomplete; automatic cleanup paused"}</AlertTitle>
            <AlertDescription>{inventory?.referenceError || (zh ? "人工删除仍可继续，风险由操作者确认承担。" : "Manual deletion remains available after operator confirmation.")}</AlertDescription>
          </Alert>
        ) : null}
        {error ? (
          <Alert variant="destructive">
            <AlertTriangle />
            <AlertTitle>{zh ? "操作失败" : "Operation failed"}</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : null}
        {usage.error ? (
          <Alert>
            <AlertTriangle />
            <AlertTitle>{zh ? "容量数据暂不可用" : "Storage data unavailable"}</AlertTitle>
            <AlertDescription>
              {usageInterrupted
                ? (zh ? "节点 agent 在容量采集完成前重启，本次扫描未取得容量数据。镜像清单仍可查看；节点恢复后可重新扫描。" : "The node agent restarted before storage inspection completed. Image inventory remains available; rescan after the node recovers.")
                : usage.error}
            </AlertDescription>
            <AlertAction><Button type="button" variant="outline" size="sm" disabled={loading || !!busy} onClick={() => void load(true)}>{zh ? "重新采集" : "Retry scan"}</Button></AlertAction>
          </Alert>
        ) : null}
        {notice ? (
          <Alert>
            <ShieldCheck />
            <AlertTitle>{notice}</AlertTitle>
            <AlertAction>
              <Button type="button" variant="ghost" size="sm" onClick={() => setNotice("")}>{zh ? "关闭" : "Dismiss"}</Button>
            </AlertAction>
          </Alert>
        ) : null}

        {section === "inventory" ? <>
        <section className="registry-usage-grid" aria-label={zh ? "镜像仓库容量" : "Registry storage usage"}>
          <article className={`registry-usage-card disk-${diskTone}`}>
            <div><HardDrive size={20} /><span>{zh ? "宿主磁盘" : "Host filesystem"}</span></div>
            <strong>{hasDiskPercent ? `${diskPercent}%` : unavailable}</strong>
            <small>{zh ? `可用 ${usageBytes(usage.filesystemAvailableBytes)}` : `${usageBytes(usage.filesystemAvailableBytes)} available`}</small>
            {hasDiskPercent ? <i style={{ "--registry-meter": `${Math.min(diskPercent, 100)}%` } as CSSProperties} /> : null}
          </article>
          <article className="registry-usage-card">
            <div><Database size={20} /><span>{zh ? "镜像数据卷" : "Registry volume"}</span></div>
            <strong>{usageBytes(usage.volumeBytes)}</strong>
            <small>{inventory?.registry?.volumeName || "-"}</small>
          </article>
          <article className="registry-usage-card">
            <div><ShieldCheck size={20} /><span>{zh ? "受保护 manifest" : "Protected manifests"}</span></div>
            <strong>{summary.protectedCount || 0}</strong>
            <small>{zh ? "运行、回滚、构建与系统引用" : "Runtime, rollback, build, and system references"}</small>
          </article>
          <article className="registry-usage-card">
            <div><Clock3 size={20} /><span>{zh ? "最近扫描" : "Last scan"}</span></div>
            <strong>{summary.durationMs ? `${(summary.durationMs / 1000).toFixed(1)}s` : "-"}</strong>
            <small>{formatTimestamp(summary.scannedAt, lang)}</small>
          </article>
        </section>

        <Card>
          <CardHeader>
            <CardTitle>{zh ? "Registry 存储月度分布" : "Registry storage by month"}</CardTitle>
            <CardDescription>{zh ? "现存 Blob 按文件最后修改月份汇总，展示最近 6 个有数据的月份；不是每月总容量快照。" : "Existing blobs grouped by file modification month, showing the latest six populated months; not historical capacity snapshots."}</CardDescription>
          </CardHeader>
          <CardContent>
            {hasUsage && monthly.length ? <div className="registry-growth-bars">{monthly.map((item) => <span key={item.month}><i aria-hidden="true" style={{ height: `${(Number(item.bytes || 0) / monthlyMax) * 100}%` }} /><strong>{formatBytes(item.bytes)}</strong><small>{item.month}</small></span>)}</div>
              : <p className="text-sm text-muted-foreground">{usage.error
                ? (zh ? "容量采集失败，暂无法显示月度分布。请重新采集后查看。" : "Storage collection failed. Retry the scan to load monthly data.")
                : (zh ? "暂无月度数据，重新扫描后显示存储分布。" : "No monthly data yet. Rescan to load the storage distribution.")}</p>}
          </CardContent>
        </Card>

        </> : null}

        {showPolicy ? (
          <section className="panel registry-policy-panel">
            <div className="panel-heading"><div><p className="eyebrow">Retention</p><h2>{zh ? "保留与安全窗口" : "Retention and safety windows"}</h2></div><Settings2 size={18} /></div>
            <div className="registry-policy-grid">
              <Field>
                <FieldLabel>{zh ? "模式" : "Mode"}</FieldLabel>
                <SelectControl
                  className="min-w-0"
                  value={policy.mode}
                  onChange={(value) => setPolicy({ ...policy, mode: value as RegistryPolicy["mode"] })}
                  options={[
                    { value: "off", label: "Off" },
                    { value: "recommend", label: "Recommend" },
                    { value: "enforce", label: "Enforce" },
                  ]}
                />
              </Field>
              <Field>
                <FieldLabel>{zh ? "每仓库至少保留" : "Keep per repository"}</FieldLabel>
                <Input type="number" min={1} value={policy.keepLast} onChange={(event) => setPolicy({ ...policy, keepLast: Number(event.target.value) })} />
              </Field>
              <Field>
                <FieldLabel>{zh ? "保留天数" : "Max age days"}</FieldLabel>
                <Input type="number" min={1} value={policy.maxAgeDays} onChange={(event) => setPolicy({ ...policy, maxAgeDays: Number(event.target.value) })} />
              </Field>
              <Field>
                <FieldLabel>{zh ? "系统版本保留" : "System versions"}</FieldLabel>
                <Input type="number" min={1} value={policy.systemKeepLast} onChange={(event) => setPolicy({ ...policy, systemKeepLast: Number(event.target.value) })} />
              </Field>
              <Field>
                <FieldLabel>{zh ? "删除宽限期（小时，0 为立即执行）" : "Queue grace hours (0 = immediate)"}</FieldLabel>
                <Input type="number" min={0} value={policy.queueGraceHours} onChange={(event) => setPolicy({ ...policy, queueGraceHours: Number(event.target.value) })} />
              </Field>
              <Field>
                <FieldLabel>{zh ? "GC 恢复窗口（天）" : "GC grace days"}</FieldLabel>
                <Input type="number" min={1} value={policy.gcGraceDays} onChange={(event) => setPolicy({ ...policy, gcGraceDays: Number(event.target.value) })} />
              </Field>
              <Field>
                <FieldLabel>{zh ? "容量预警（%）" : "Warning usage (%)"}</FieldLabel>
                <Input type="number" min={1} max={99} value={policy.warningPercent} onChange={(event) => setPolicy({ ...policy, warningPercent: Number(event.target.value) })} />
              </Field>
              <Field>
                <FieldLabel>{zh ? "容量严重（%）" : "Critical usage (%)"}</FieldLabel>
                <Input type="number" min={2} max={100} value={policy.criticalPercent} onChange={(event) => setPolicy({ ...policy, criticalPercent: Number(event.target.value) })} />
              </Field>
              <Field>
                <FieldLabel>{zh ? "容量紧急（%）" : "Emergency usage (%)"}</FieldLabel>
                <Input type="number" min={3} max={100} value={policy.emergencyPercent} onChange={(event) => setPolicy({ ...policy, emergencyPercent: Number(event.target.value) })} />
              </Field>
            </div>
            {policy.mode === "enforce" ? <div className="registry-policy-warning"><AlertTriangle size={16} /><span>{policy.queueGraceHours > 0 ? (zh ? "Enforce 会自动把候选 manifest 加入队列，宽限期后删除，并在恢复窗口结束后执行离线 GC。" : "Enforce automatically queues candidates, deletes them after the grace period, and runs offline GC after the recovery window.") : (zh ? "Enforce 会自动把候选 manifest 加入队列并立即删除；最终 GC 仍会等待恢复窗口结束。" : "Enforce automatically queues and immediately deletes candidates; final GC still waits for the recovery window.")}</span></div> : null}
            <div className="registry-policy-actions"><Button type="button" variant="outline" onClick={() => setShowPolicy(false)}>{zh ? "取消" : "Cancel"}</Button><Button type="button" disabled={busy === "policy"} onClick={() => void savePolicy()}>{busy === "policy" ? (zh ? "保存中…" : "Saving…") : (zh ? "保存策略" : "Save policy")}</Button></div>
          </section>
        ) : null}

        {section === "inventory" ? <section className="panel registry-inventory-panel">
          <div className="registry-inventory-toolbar">
            <InputGroup className="max-w-xl">
              <InputGroupAddon>
                <Search />
              </InputGroupAddon>
              <InputGroupInput value={query} onChange={(event) => setQuery(event.target.value)} placeholder={zh ? "搜索仓库、tag 或 digest" : "Search repository, tag, or digest"} />
            </InputGroup>
            <Tabs value={filter} onValueChange={(value) => setFilter(String(value))}><TabsList aria-label={zh ? "镜像保护状态" : "Image protection status"}>
              {["all", "protected", "retained", "candidate", "unknown"].map((value) => <TabsTrigger key={value} value={value}>{value === "all" ? (zh ? "全部" : "All") : statusLabel(value, zh)}</TabsTrigger>)}
            </TabsList></Tabs>
            <Button variant="destructive" type="button" disabled={!selected.size || !!busy} onClick={() => void openDeletePreview()}><Trash2 size={15} /> {zh ? `删除镜像${selected.size ? `（${selected.size}）` : ""}` : `Delete images${selected.size ? ` (${selected.size})` : ""}`}</Button>
          </div>
          <div className="table-wrap registry-table-wrap" tabIndex={0} role="region" aria-label={zh ? "镜像列表，可横向滚动" : "Image inventory, horizontally scrollable"}>
            <table className="registry-table">
              <thead><tr><th><input type="checkbox" checked={allVisibleSelected} aria-label={zh ? "选择所有可操作项" : "Select all actionable items"} onChange={() => setSelected((current) => { const next = new Set(current); selectable.forEach((item) => allVisibleSelected ? next.delete(keyFor(item)) : next.add(keyFor(item))); return next; })} /></th><th>{zh ? "仓库 / Digest" : "Repository / Digest"}</th><th>Tags</th><th>{zh ? "平台" : "Platforms"}</th><th>{zh ? "体积" : "Size"}</th><th>{zh ? "创建时间" : "Created"}</th><th>{zh ? "保护状态" : "Protection"}</th></tr></thead>
              <tbody>
                {entries.map((item) => {
                  return <tr key={keyFor(item)} className={selected.has(keyFor(item)) ? "selected" : ""}>
                    <td><input type="checkbox" checked={selected.has(keyFor(item))} onChange={() => toggle(item)} aria-label={`${item.repository} ${item.digest}`} /></td>
                    <td><span className="registry-repository"><a href={toHref(`/registry/image?image=${encodeURIComponent(keyFor(item))}`)} onClick={(event) => { if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); navigate(`/registry/image?image=${encodeURIComponent(keyFor(item))}`); }}>{item.repository}</a><CodeCell value={item.digest} /></span></td>
                    <td><span className="registry-tags">{(item.tags || []).slice(0, 4).map((tag) => <code key={tag}>{tag}</code>)}{(item.tags || []).length > 4 ? <small>+{item.tags.length - 4}</small> : null}</span></td>
                    <td><span className="registry-platforms">{(item.platforms || []).length ? item.platforms?.map((platform) => <small key={platform}>{platform}</small>) : <small>-</small>}</span></td>
                    <td>{formatBytes(item.logicalBytes)}</td>
                    <td>{formatTimestamp(item.createdAt || item.lastModified, lang)}</td>
                    <td><span className="registry-protection"><StatePill label={statusLabel(item.protectionStatus, zh)} value={item.protectionStatus === "protected" ? "ready" : item.protectionStatus === "candidate" ? "warning" : item.protectionStatus} />{item.protectionReasons?.slice(0, 2).map((reason, index) => <small key={`${reason.kind}-${index}`}>{reason.source || reason.kind}</small>)}</span></td>
                  </tr>;
                })}
                {!entries.length ? <tr><td colSpan={7} className="registry-empty">{loading ? (zh ? "正在扫描 Registry…" : "Scanning Registry…") : (zh ? "没有匹配的镜像" : "No matching images")}</td></tr> : null}
              </tbody>
            </table>
          </div>
          {inventory?.page?.hasMore ? <div className="registry-load-more"><Button variant="outline" type="button" disabled={loading || !!busy} onClick={() => void load(false, (inventory.page?.offset || 0) + (inventory.page?.limit || REGISTRY_PAGE_SIZE), true)}>{loading ? (zh ? "加载中…" : "Loading…") : (zh ? `加载更多（已显示 ${entries.length} / ${inventory.page.total || 0}）` : `Load more (${entries.length} / ${inventory.page.total || 0})`)}</Button></div> : null}
        </section> : null}

      </main>

      {section === "image" ? <section className="panel registry-image-detail">
        <Button variant="outline" type="button" onClick={() => navigate("/registry")}>{zh ? "返回镜像" : "Back to images"}</Button>
        {detail ? <><h2>{detail.repository}</h2><CodeCell value={detail.digest} /><dl className="detail-grid">
          <div><dt>Tags</dt><dd>{detail.tags?.join(", ") || "—"}</dd></div>
          <div><dt>{zh ? "平台" : "Platforms"}</dt><dd>{detail.platforms?.join(", ") || "—"}</dd></div>
          <div><dt>{zh ? "逻辑大小" : "Logical size"}</dt><dd>{formatBytes(detail.logicalBytes)}</dd></div>
          <div><dt>{zh ? "创建时间" : "Created"}</dt><dd>{formatTimestamp(detail.createdAt || detail.lastModified, lang)}</dd></div>
          <div><dt>{zh ? "保护状态" : "Protection"}</dt><dd>{statusLabel(detail.protectionStatus, zh)}</dd></div>
          <div><dt>{zh ? "媒体类型" : "Media type"}</dt><dd>{detail.mediaType || "—"}</dd></div>
        </dl><h3>{zh ? "引用关系" : "References"}</h3>{detail.protectionReasons?.length ? <ul>{detail.protectionReasons.map((reason, index) => <li key={index}>{reason.kind} · {reason.source} · {reason.reference}</li>)}</ul> : <p>{zh ? "没有已知引用" : "No known references"}</p>} <h3>{zh ? "平台 manifests" : "Platform manifests"}</h3>{detail.childManifestDigests?.map((digest) => <CodeCell key={digest} value={digest} />)}</> : <div role={detailStatus === "error" ? "alert" : "status"} aria-busy={detailStatus === "loading" || detailStatus === "pending"}>
          <p>{detailStatus === "loading" ? (zh ? "正在查询镜像…" : "Loading image…") : detailStatus === "pending" ? (zh ? "镜像索引正在建立，完成后将自动刷新。" : "Building image index; this page will refresh when ready.") : detailStatus === "error" ? (zh ? "镜像查询失败。" : "Image lookup failed.") : (zh ? "未找到此镜像，它可能已经删除或不在当前索引中。" : "Image not found. It may have been deleted or is absent from the current index.")}</p>
          {detailStatus === "error" ? <p>{detailState.error}</p> : null}
          {detailStatus === "error" || detailStatus === "missing" ? <Button variant="outline" type="button" onClick={() => setDetailRevision((value) => value + 1)}>{zh ? "重新查询" : "Retry lookup"}</Button> : null}
        </div>}
      </section> : null}
      {section === "delete" && !preview ? <section className="panel registry-image-detail"><h2>{zh ? "重新选择清理范围" : "Select cleanup scope"}</h2><p>{zh ? "为确保清理范围准确，刷新页面后需要重新选择镜像并分析。" : "After refreshing, select images and preview again to confirm the exact cleanup scope."}</p><Button type="button" onClick={() => navigate("/registry")}>{zh ? "返回镜像列表" : "Back to images"}</Button></section> : null}
      {section === "delete" && preview ? (
          <Card className="registry-delete-page" aria-labelledby="registry-delete-title" aria-busy={busy === "purge"}>
            <CardHeader>
              <CardTitle id="registry-delete-title">{zh ? "清理范围" : "Cleanup scope"}</CardTitle>
              <CardDescription>
                {zh
                  ? `将删除以下 ${preview.selected?.length || 0} 个镜像及其全部标签，并清理不再使用的镜像数据。`
                  : `Delete the ${preview.selected?.length || 0} images below and all their tags, then remove their unused image data.`}
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <div className="registry-delete-table" tabIndex={0} role="region" aria-label={zh ? "待删除镜像，可横向滚动" : "Selected images, horizontally scrollable"}>
                <Table>
                  <TableHeader><TableRow>
                    <TableHead>{zh ? "标签" : "Tags"}</TableHead>
                    <TableHead>{zh ? "镜像摘要" : "Digest"}</TableHead>
                    <TableHead>{zh ? "逻辑体积" : "Logical size"}</TableHead>
                  </TableRow></TableHeader>
                  {[...deletionRepositories].map(([repository, images]) => (
                    <TableBody key={repository}>
                      <TableRow><TableHead colSpan={3} scope="rowgroup"><span className="flex items-center gap-2"><Boxes aria-hidden="true" className="size-4 shrink-0" /><span>{repository}</span><Badge variant="secondary">{images.length}</Badge></span></TableHead></TableRow>
                      {images.map((item) => <TableRow key={keyFor(item)}>
                        <TableCell><span className="flex flex-wrap gap-1">{item.tags?.length ? item.tags.map((tag) => <Badge key={tag} variant="outline">{tag}</Badge>) : <span className="text-muted-foreground">{zh ? "无标签" : "Untagged"}</span>}</span></TableCell>
                        <TableCell><code className="block truncate font-mono text-xs" title={item.digest}>{item.digest}</code></TableCell>
                        <TableCell>{formatBytes(item.logicalBytes)}</TableCell>
                      </TableRow>)}
                    </TableBody>
                  ))}
                </Table>
              </div>
              <Alert>
                <Info />
                <AlertTitle>{zh ? "释放空间以清理结果为准" : "Freed space is measured after cleanup"}</AlertTitle>
                <AlertDescription>{zh ? "其他镜像仍在使用的共享层会保留。清理完成后将显示实际释放空间。" : "Layers still used by other images are retained. Actual reclaimed space is shown when cleanup completes."}</AlertDescription>
              </Alert>
              <Alert variant="destructive">
                <AlertTriangle />
                <AlertTitle>{zh ? "删除后无法恢复" : "Deletion is permanent"}</AlertTitle>
                <AlertDescription>{zh ? "此操作不创建备份。删除与回收期间 Registry 暂停服务，无法推送或拉取镜像。" : "No backup is created. The registry is unavailable for pushes and pulls during deletion and reclamation."}</AlertDescription>
              </Alert>
              {preview.risks?.length ? <Alert variant="destructive">
                <AlertTriangle />
                <AlertTitle>{zh ? "存在引用风险" : "Referenced images are included"}</AlertTitle>
                <AlertDescription>{zh ? "部分镜像仍有服务引用，或引用扫描不完整。删除后，相关服务重启或回滚时可能无法拉取镜像。" : "Some images remain referenced, or reference scanning is incomplete. Affected services may fail to pull their image on restart or rollback."}</AlertDescription>
              </Alert> : null}
              {preview.blocked?.length ? <Alert variant="destructive">
                <AlertTriangle />
                <AlertTitle>{zh ? "当前范围无法执行" : "This selection cannot be processed"}</AlertTitle>
                <AlertDescription>{zh ? `${preview.blocked.length} 项因镜像不存在或批量范围过大而无法处理，请返回调整。` : `${preview.blocked.length} items are missing or exceed the batch limit. Go back to adjust the selection.`}</AlertDescription>
              </Alert> : null}
              {busy === "purge" ? <Alert role="status">
                <RefreshCw className="animate-spin" />
                <AlertTitle>{zh ? "正在删除镜像" : "Deleting images"}</AlertTitle>
                <AlertDescription>{zh ? `已等待 ${purgeElapsed} 秒。正在删除镜像并清理空间，完成后会自动返回列表。` : `Elapsed: ${purgeElapsed}s. Deleting images and freeing space; you will return to the list when complete.`}</AlertDescription>
              </Alert> : null}
            </CardContent>
            <CardFooter className="flex-wrap justify-end gap-2">
              <Button variant="outline" type="button" disabled={!!busy} onClick={() => { setPreview(null); navigate("/registry"); }}>
                {zh ? "取消" : "Cancel"}
              </Button>
              <Button variant="destructive" type="button" disabled={!preview.allowed || !!busy} onClick={() => void purgeSelection()}>
                {busy === "purge" ? <RefreshCw data-icon="inline-start" className="animate-spin" /> : <Trash2 data-icon="inline-start" />}
                {busy === "purge"
                  ? (zh ? "正在删除…" : "Deleting…")
                  : (zh ? "确认删除" : "Delete images")}
              </Button>
            </CardFooter>
          </Card>

      ) : null}
    </div>
  );
}
