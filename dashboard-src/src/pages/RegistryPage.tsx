import {
  AlertTriangle,
  Boxes,
  Clock3,
  Database,
  HardDrive,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  Settings2,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import { CodeCell, StatePill } from "../components/ui";
import { formatTimestamp } from "../format";
import {
  createRegistryDeletion,
  fetchRegistryInventory,
  previewRegistryDeletion,
  registryDeletionAction,
  registryGc,
  saveRegistryPolicy,
  type RegistryDeletion,
  type RegistryInventory,
  type RegistryManifest,
  type RegistryPolicy,
} from "../registryManagementApi";
import type { Lang } from "../types";
import { PageHeader } from "./PageHeader";

const DEFAULT_POLICY: RegistryPolicy = {
  mode: "recommend",
  keepLast: 20,
  maxAgeDays: 30,
  systemKeepLast: 3,
  queueGraceHours: 24,
  gcGraceDays: 7,
  warningPercent: 75,
  criticalPercent: 85,
  emergencyPercent: 92,
};

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

function deletionStatusLabel(status: string, zh: boolean) {
  const labels: Record<string, [string, string]> = {
    queued: ["等待执行", "Queued"],
    deleting: ["删除中", "Deleting"],
    deleted_pending_gc: ["可恢复 · 待 GC", "Recoverable · GC pending"],
    restored: ["已恢复", "Restored"],
    gc_completed: ["已回收", "Reclaimed"],
    canceled: ["已取消", "Canceled"],
    failed: ["失败", "Failed"],
    failed_recoverable: ["失败 · 可恢复", "Failed · Recoverable"],
  };
  const pair = labels[status] || [status, status];
  return zh ? pair[0] : pair[1];
}

export function RegistryPage({ lang, token }: { lang: Lang; token: string }) {
  const zh = lang === "zh";
  const [inventory, setInventory] = useState<RegistryInventory | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof previewRegistryDeletion>> | null>(null);
  const [showPolicy, setShowPolicy] = useState(false);
  const [policy, setPolicy] = useState<RegistryPolicy>(DEFAULT_POLICY);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError("");
    try {
      const next = await fetchRegistryInventory(token, refresh);
      setInventory(next);
      setPolicy(next.policy || DEFAULT_POLICY);
      setSelected((current) => {
        const available = new Set((next.entries || []).map(keyFor));
        return new Set([...current].filter((key) => available.has(key)));
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void load(false);
  }, [load]);

  const entries = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (inventory?.entries || []).filter((item) => {
      if (filter !== "all" && item.protectionStatus !== filter) return false;
      if (!needle) return true;
      return `${item.repository} ${(item.tags || []).join(" ")} ${item.digest}`.toLowerCase().includes(needle);
    });
  }, [filter, inventory?.entries, query]);

  const selectable = entries.filter((item) => !item.protectionReasons?.length && item.protectionStatus !== "unknown");
  const allVisibleSelected = selectable.length > 0 && selectable.every((item) => selected.has(keyFor(item)));
  const summary = inventory?.summary || {};
  const usage = inventory?.usage || {};
  const diskPercent = Number(usage.filesystemUsePercent || 0);
  const diskTone = diskPercent >= (policy.emergencyPercent || 92) ? "critical" : diskPercent >= (policy.criticalPercent || 85) ? "warning" : "healthy";
  const monthly = (usage.monthlyBlobs || []).slice(-6);
  const monthlyMax = Math.max(...monthly.map((item) => Number(item.bytes || 0)), 1);

  const selectionItems = (inventory?.entries || []).filter((item) => selected.has(keyFor(item)));

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
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const queueDeletion = async () => {
    if (!preview?.allowed) return;
    setBusy("queue");
    try {
      await createRegistryDeletion(token, selectionItems.map(({ repository, digest }) => ({ repository, digest })));
      setPreview(null);
      setSelected(new Set());
      setNotice(zh ? "已加入清理队列；宽限期内可以取消。" : "Added to the cleanup queue; it can be canceled during the grace period.");
      await load(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const runDeletionAction = async (deletion: RegistryDeletion, action: "cancel" | "execute" | "restore", force = false) => {
    setBusy(`${action}-${deletion.id}`);
    setError("");
    try {
      await registryDeletionAction(token, deletion.id, action, force);
      setNotice(zh ? "Registry 操作已完成。" : "Registry operation completed.");
      await load(true);
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
      await load(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const runGc = async (execute: boolean) => {
    if (execute && !window.confirm(zh ? "执行 GC 后不可恢复，确认继续？" : "GC is irreversible. Continue?")) return;
    setBusy(execute ? "gc" : "gc-preview");
    setError("");
    try {
      const result = await registryGc(token, execute);
      const payload = (execute ? result.result : result.preview) || {};
      setNotice(
        execute
          ? (zh ? `GC 完成，释放 ${formatBytes(Number(payload.reclaimedBytes || 0))}。` : `GC completed; reclaimed ${formatBytes(Number(payload.reclaimedBytes || 0))}.`)
          : (zh ? `GC 预检完成：${Number(payload.eligibleBlobs || 0)} 个 blob 可回收。` : `GC preview: ${Number(payload.eligibleBlobs || 0)} blobs are eligible.`),
      );
      await load(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  return (
    <>
      <PageHeader
        meta={{
          eyebrow: zh ? "镜像生命周期" : "Image lifecycle",
          title: zh ? "Registry 镜像管理" : "Registry image management",
          description: zh
            ? "按 digest 追踪引用、排队删除并在受控维护窗口回收空间。"
            : "Track references by digest, queue deletions, and reclaim space in controlled maintenance windows.",
          metrics: [
            { label: zh ? "仓库" : "Repositories", value: summary.repositoryCount || 0 },
            { label: "Tags", value: summary.tagCount || 0 },
            { label: zh ? "候选" : "Candidates", value: summary.candidateCount || 0 },
          ],
          action: (
            <div className="registry-header-actions">
              <button type="button" className="ghost" disabled={loading || !!busy} onClick={() => void load(true)}>
                <RefreshCw size={15} className={loading ? "spin" : ""} /> {zh ? "重新扫描" : "Rescan"}
              </button>
              <button type="button" className="ghost" onClick={() => setShowPolicy((value) => !value)}>
                <Settings2 size={15} /> {zh ? "保留策略" : "Retention"}
              </button>
            </div>
          ),
        }}
      />

      <main className="registry-page">
        {!inventory?.protectionComplete ? (
          <div className="registry-alert critical" role="alert">
            <AlertTriangle size={18} />
            <span><strong>{zh ? "引用扫描不完整，删除已锁定" : "Reference scan incomplete; deletion locked"}</strong><small>{inventory?.referenceError || (zh ? "等待 Nomad 返回完整 job version。" : "Waiting for complete Nomad job versions.")}</small></span>
          </div>
        ) : null}
        {error ? <div className="registry-alert critical" role="alert"><AlertTriangle size={18} /><span><strong>{zh ? "操作失败" : "Operation failed"}</strong><small>{error}</small></span></div> : null}
        {usage.error ? <div className="registry-alert warning" role="alert"><AlertTriangle size={18} /><span><strong>{zh ? "容量数据暂不可用" : "Storage data unavailable"}</strong><small>{usage.error}</small></span></div> : null}
        {notice ? <div className="registry-alert success"><ShieldCheck size={18} /><span><strong>{notice}</strong></span><button type="button" onClick={() => setNotice("")}>×</button></div> : null}

        <section className="registry-usage-grid">
          <article className={`registry-usage-card disk-${diskTone}`}>
            <div><HardDrive size={20} /><span>{zh ? "宿主磁盘" : "Host filesystem"}</span></div>
            <strong>{diskPercent ? `${diskPercent}%` : "-"}</strong>
            <small>{zh ? `可用 ${formatBytes(usage.filesystemAvailableBytes)}` : `${formatBytes(usage.filesystemAvailableBytes)} available`}</small>
            <i style={{ "--registry-meter": `${Math.min(diskPercent, 100)}%` } as CSSProperties} />
          </article>
          <article className="registry-usage-card">
            <div><Database size={20} /><span>{zh ? "镜像数据卷" : "Registry volume"}</span></div>
            <strong>{formatBytes(usage.volumeBytes)}</strong>
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

        {monthly.length ? <section className="panel registry-growth-panel"><div><p className="eyebrow">Blob growth</p><h2>{zh ? "近月写入分布" : "Recent blob writes"}</h2><small>{zh ? "按 blob 文件最后修改月份统计，用于观察增长趋势。" : "Grouped by blob file modification month to expose growth trends."}</small></div><div className="registry-growth-bars">{monthly.map((item) => <span key={item.month}><i style={{ height: `${Math.max((Number(item.bytes || 0) / monthlyMax) * 100, 4)}%` }} /><strong>{formatBytes(item.bytes)}</strong><small>{item.month}</small></span>)}</div></section> : null}

        {showPolicy ? (
          <section className="panel registry-policy-panel">
            <div className="panel-heading"><div><p className="eyebrow">Retention</p><h2>{zh ? "保留与安全窗口" : "Retention and safety windows"}</h2></div><Settings2 size={18} /></div>
            <div className="registry-policy-grid">
              <label><span>{zh ? "模式" : "Mode"}</span><select value={policy.mode} onChange={(event) => setPolicy({ ...policy, mode: event.target.value as RegistryPolicy["mode"] })}><option value="off">Off</option><option value="recommend">Recommend</option><option value="enforce">Enforce</option></select></label>
              <label><span>{zh ? "每仓库至少保留" : "Keep per repository"}</span><input type="number" min={1} value={policy.keepLast} onChange={(event) => setPolicy({ ...policy, keepLast: Number(event.target.value) })} /></label>
              <label><span>{zh ? "保留天数" : "Max age days"}</span><input type="number" min={1} value={policy.maxAgeDays} onChange={(event) => setPolicy({ ...policy, maxAgeDays: Number(event.target.value) })} /></label>
              <label><span>{zh ? "系统版本保留" : "System versions"}</span><input type="number" min={1} value={policy.systemKeepLast} onChange={(event) => setPolicy({ ...policy, systemKeepLast: Number(event.target.value) })} /></label>
              <label><span>{zh ? "删除宽限期（小时）" : "Queue grace hours"}</span><input type="number" min={1} value={policy.queueGraceHours} onChange={(event) => setPolicy({ ...policy, queueGraceHours: Number(event.target.value) })} /></label>
              <label><span>{zh ? "GC 恢复窗口（天）" : "GC grace days"}</span><input type="number" min={1} value={policy.gcGraceDays} onChange={(event) => setPolicy({ ...policy, gcGraceDays: Number(event.target.value) })} /></label>
              <label><span>{zh ? "容量预警（%）" : "Warning usage (%)"}</span><input type="number" min={1} max={99} value={policy.warningPercent} onChange={(event) => setPolicy({ ...policy, warningPercent: Number(event.target.value) })} /></label>
              <label><span>{zh ? "容量严重（%）" : "Critical usage (%)"}</span><input type="number" min={2} max={100} value={policy.criticalPercent} onChange={(event) => setPolicy({ ...policy, criticalPercent: Number(event.target.value) })} /></label>
              <label><span>{zh ? "容量紧急（%）" : "Emergency usage (%)"}</span><input type="number" min={3} max={100} value={policy.emergencyPercent} onChange={(event) => setPolicy({ ...policy, emergencyPercent: Number(event.target.value) })} /></label>
            </div>
            {policy.mode === "enforce" ? <div className="registry-policy-warning"><AlertTriangle size={16} /><span>{zh ? "Enforce 会自动把候选 manifest 加入队列，宽限期后删除，并在恢复窗口结束后执行离线 GC。" : "Enforce automatically queues candidates, deletes them after the grace period, and runs offline GC after the recovery window."}</span></div> : null}
            <div className="registry-policy-actions"><button type="button" className="ghost" onClick={() => setShowPolicy(false)}>{zh ? "取消" : "Cancel"}</button><button type="button" className="primary" disabled={busy === "policy"} onClick={() => void savePolicy()}>{busy === "policy" ? (zh ? "保存中…" : "Saving…") : (zh ? "保存策略" : "Save policy")}</button></div>
          </section>
        ) : null}

        <section className="panel registry-inventory-panel">
          <div className="registry-inventory-toolbar">
            <div className="registry-search"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={zh ? "搜索仓库、tag 或 digest" : "Search repository, tag, or digest"} /></div>
            <div className="registry-filters">
              {["all", "protected", "retained", "candidate", "unknown"].map((value) => <button type="button" key={value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)}>{value === "all" ? (zh ? "全部" : "All") : statusLabel(value, zh)}</button>)}
            </div>
            <button type="button" className="danger" disabled={!selected.size || !!busy} onClick={() => void openDeletePreview()}><Trash2 size={15} /> {zh ? `清理 ${selected.size} 项` : `Clean ${selected.size}`}</button>
          </div>
          <div className="table-wrap registry-table-wrap">
            <table className="registry-table">
              <thead><tr><th><input type="checkbox" checked={allVisibleSelected} aria-label={zh ? "选择所有可操作项" : "Select all actionable items"} onChange={() => setSelected((current) => { const next = new Set(current); selectable.forEach((item) => allVisibleSelected ? next.delete(keyFor(item)) : next.add(keyFor(item))); return next; })} /></th><th>{zh ? "仓库 / Digest" : "Repository / Digest"}</th><th>Tags</th><th>{zh ? "平台" : "Platforms"}</th><th>{zh ? "体积" : "Size"}</th><th>{zh ? "创建时间" : "Created"}</th><th>{zh ? "保护状态" : "Protection"}</th></tr></thead>
              <tbody>
                {entries.map((item) => {
                  const disabled = !!item.protectionReasons?.length || item.protectionStatus === "unknown";
                  return <tr key={keyFor(item)} className={selected.has(keyFor(item)) ? "selected" : ""}>
                    <td><input type="checkbox" disabled={disabled} checked={selected.has(keyFor(item))} onChange={() => toggle(item)} aria-label={`${item.repository} ${item.digest}`} /></td>
                    <td><span className="registry-repository"><strong>{item.repository}</strong><CodeCell value={item.digest.replace("sha256:", "sha256:​")} /></span></td>
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
        </section>

        <section className="registry-lifecycle-grid">
          <article className="panel registry-queue-panel">
            <div className="panel-heading"><div><p className="eyebrow">Cleanup queue</p><h2>{zh ? "清理队列" : "Cleanup queue"}</h2></div><Clock3 size={18} /></div>
            <div className="registry-queue-list">
              {(inventory?.deletions || []).slice(0, 12).map((deletion) => <div className="registry-queue-row" key={deletion.id}>
                <span><strong>{deletion.manifests?.[0]?.repository || deletion.id}</strong><small>{deletion.manifests?.length || 0} manifests · {formatBytes(deletion.logicalBytes)}</small><small>{deletion.message}</small></span>
                <span><StatePill label={deletionStatusLabel(deletion.status, zh)} value={deletion.status.startsWith("failed") ? "failed" : deletion.status === "deleted_pending_gc" ? "warning" : deletion.status === "gc_completed" || deletion.status === "restored" ? "ready" : "pending"} /><small>{formatTimestamp(deletion.updatedAt || deletion.createdAt, lang)}</small></span>
                <div>
                  {deletion.status === "queued" ? <><button type="button" className="ghost" disabled={!!busy} onClick={() => void runDeletionAction(deletion, "cancel")}>{zh ? "取消" : "Cancel"}</button><button type="button" className="ghost danger" disabled={!!busy || Number(deletion.notBefore || 0) > Date.now() / 1000} onClick={() => void runDeletionAction(deletion, "execute")}>{zh ? "执行" : "Execute"}</button></> : null}
                  {deletion.status === "deleted_pending_gc" || deletion.status === "failed_recoverable" ? <button type="button" className="ghost" disabled={!!busy} onClick={() => void runDeletionAction(deletion, "restore")}><RotateCcw size={14} /> {zh ? "恢复" : "Restore"}</button> : null}
                </div>
              </div>)}
              {!(inventory?.deletions || []).length ? <div className="registry-empty-state"><Boxes size={22} /><span>{zh ? "清理队列为空" : "Cleanup queue is empty"}</span></div> : null}
            </div>
          </article>
          <article className="panel registry-gc-panel">
            <div className="panel-heading"><div><p className="eyebrow">Garbage collection</p><h2>{zh ? "空间回收" : "Space reclamation"}</h2></div><Database size={18} /></div>
            <p>{zh ? "GC 会短暂停止 Registry，扫描所有 manifest 引用后删除失去引用的 blob。执行前始终可以先做预检。" : "GC briefly stops Registry, marks every manifest reference, then removes unreachable blobs. Always preview first."}</p>
            <div className="registry-gc-actions"><button type="button" className="ghost" disabled={!!busy} onClick={() => void runGc(false)}><Search size={15} /> {busy === "gc-preview" ? (zh ? "预检中…" : "Previewing…") : (zh ? "GC 预检" : "Preview GC")}</button><button type="button" className="danger" disabled={!!busy || !(inventory?.deletions || []).some((item) => item.status === "deleted_pending_gc" && Number(item.gcAfter || 0) <= Date.now() / 1000)} onClick={() => void runGc(true)}><Play size={15} /> {busy === "gc" ? (zh ? "回收中…" : "Reclaiming…") : (zh ? "执行 GC" : "Run GC")}</button></div>
          </article>
        </section>
      </main>

      {preview ? <div className="registry-dialog-backdrop" role="presentation" onMouseDown={() => !busy && setPreview(null)}><section className="registry-dialog" role="dialog" aria-modal="true" aria-labelledby="registry-delete-title" onMouseDown={(event) => event.stopPropagation()}><div className="registry-dialog-icon"><Trash2 size={22} /></div><h2 id="registry-delete-title">{zh ? "加入清理队列" : "Add to cleanup queue"}</h2><p>{zh ? `将 ${preview.selected?.length || 0} 个顶层 manifest 加入队列，并安全处理 ${preview.dependentManifests?.length || 0} 个仅由它们引用的平台 manifest。所有指向同一 digest 的 tag 会一起删除。` : `Queue ${preview.selected?.length || 0} root manifests and safely include ${preview.dependentManifests?.length || 0} platform manifests referenced only by them. Every tag pointing to the same digest will be deleted together.`}</p><div className="registry-dialog-summary"><span><strong>{preview.selected?.reduce((count, item) => count + (item.tags?.length || 0), 0) || 0}</strong><small>tags</small></span><span><strong>{formatBytes(preview.logicalBytes)}</strong><small>{zh ? "逻辑体积" : "logical size"}</small></span><span><strong>{policy.queueGraceHours}h</strong><small>{zh ? "可取消窗口" : "cancel window"}</small></span></div>{preview.blocked?.length ? <div className="registry-dialog-blocked"><AlertTriangle size={16} /> {zh ? `${preview.blocked.length} 项被引用保护拦截。` : `${preview.blocked.length} items are blocked by references.`}</div> : null}<div className="registry-dialog-actions"><button type="button" className="ghost" disabled={!!busy} onClick={() => setPreview(null)}>{zh ? "返回" : "Back"}</button><button type="button" className="danger" disabled={!preview.allowed || !!busy} onClick={() => void queueDeletion()}>{busy === "queue" ? (zh ? "排队中…" : "Queueing…") : (zh ? "确认加入队列" : "Confirm queue")}</button></div></section></div> : null}
    </>
  );
}
