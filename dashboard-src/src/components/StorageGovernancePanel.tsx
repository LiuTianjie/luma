import { useEffect, useRef, useState } from "react";
import { RefreshCw, Database, HardDrive, ShieldCheck } from "lucide-react";
import type { Lang } from "../types";
import { executeHistoryCleanup, getHistoryCleanupPlan, getStorageInventory, getStoragePolicy, getStorageTask, previewHistoryCleanup, saveStoragePolicy, startStorageTask, type HistoryCleanupPreview, type StorageInventory, type StorageOperation, type StoragePolicy, type StorageResult, type StorageTask } from "../storageGovernanceApi";
import { cleanupPlanGate, storageBytes, storageCategory, storageTaskFinished, storageTime } from "./storageGovernanceModel";
import { useConfirm } from "./ConfirmDialog";
import "./StorageGovernancePanel.css";

export function StorageGovernancePanel({ lang, token }: { lang: Lang; token: string }) {
  const zh = lang === "zh";
  const txt = (cn: string, en: string) => zh ? cn : en;
  const [inventory, setInventory] = useState<StorageInventory | null>(null);
  const [policy, setPolicy] = useState<StoragePolicy | null>(null);
  const [dirty, setDirty] = useState(false);
  const [plan, setPlan] = useState<HistoryCleanupPreview | null>(null);
  const [node, setNode] = useState("");
  const [task, setTask] = useState<StorageTask | null>(null);
  const [result, setResult] = useState<StorageResult | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState("");
  const [revision, setRevision] = useState(0);
  const [now, setNow] = useState(Date.now());
  const mounted = useRef(true);
  const requests = useRef(new Set<AbortController>());
  const { confirm, element } = useConfirm(lang);
  const bytes = (n: number | null | undefined) => storageBytes(n, txt("未测量", "Not measured"));
  const date = (value?: number | string | null) => { const ms = storageTime(value); return ms === null ? txt("暂无记录", "No record") : new Date(ms).toLocaleString(zh ? "zh-CN" : "en-US", { hour12: false }); };
  const run = async <T,>(fn: (signal: AbortSignal) => Promise<T>): Promise<T> => {
    const controller = new AbortController(); requests.current.add(controller);
    const timeout = window.setTimeout(() => controller.abort(), 20000);
    try { return await fn(controller.signal); } finally { window.clearTimeout(timeout); requests.current.delete(controller); }
  };
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; requests.current.forEach((request) => request.abort()); }; }, []);
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 10000); return () => window.clearInterval(timer); }, []);
  useEffect(() => {
    let live = true; setBusy("load"); setError("");
    void run(async (signal) => Promise.all([getStorageInventory(token, signal), getStoragePolicy(token, signal)])).then(([next, settings]) => {
      if (!live) return; setInventory(next); setPolicy(settings); setDirty(false);
      setNode((previous) => next.builders?.some((item) => item.name === previous) ? previous : next.builders?.[0]?.name || "");
    }).catch((cause) => { if (live) setError(String(cause instanceof Error ? cause.message : cause)); }).finally(() => { if (live) setBusy(""); });
    return () => { live = false; };
  }, [token, revision]);
  useEffect(() => {
    if (!task || storageTaskFinished(task.status)) return;
    let live = true, timer: number | undefined;
    const poll = async () => {
      try {
        const next = await run((signal) => getStorageTask(token, task.id, signal));
        if (!live) return;
        setTask(next.task);
        if (next.task.result) setResult(next.task.result);
        setError("");
        if (storageTaskFinished(next.task.status)) {
          if (["failed", "error", "cancelled", "expired"].includes(next.task.status)) setError(next.task.error || next.task.message || txt("任务未成功完成", "Task did not complete successfully"));
          return;
        }
      } catch (cause) { if (!live) return; setError(`${txt("任务状态读取失败，正在重试：", "Task status unavailable; retrying: ")}${cause instanceof Error ? cause.message : cause}`); }
      timer = window.setTimeout(poll, 3000);
    };
    timer = window.setTimeout(poll, 1000);
    return () => { live = false; window.clearTimeout(timer); };
  }, [token, task?.id, Boolean(task && storageTaskFinished(task.status))]);
  const action = async (key: string, fn: (signal: AbortSignal) => Promise<void>) => {
    setBusy(key); setError(""); setMessage("");
    try { await run(fn); } catch (cause) { if (mounted.current) setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { if (mounted.current) setBusy(""); }
  };
  const taskRunning = Boolean(task && !storageTaskFinished(task.status));
  const remote = async (operation: StorageOperation) => {
    if (["quarantine", "restore", "purge"].includes(operation)) {
      const verb = operation === "quarantine" ? txt("移入隔离区", "Quarantine") : operation === "purge" ? txt("永久删除", "Permanently delete") : txt("恢复文件", "Restore files");
      if (!await confirm({ title: verb, body: <>{node} · {result?.planId}<p>{operation === "quarantine" ? txt("只处理此预览计划中的文件，宽限期内可以恢复。服务器会再次检查引用与运行中构建。", "Only files in this reviewed plan are moved. They can be restored during the grace period. The server rechecks references and running builds.") : operation === "purge" ? txt("删除隔离区中已过宽限期的文件，无法撤销。", "Delete quarantined files after the grace period. This cannot be undone.") : txt("把此计划隔离的文件恢复到原位置；冲突会由服务器拦截。", "Restore files quarantined by this plan. The server blocks path conflicts.")}</p></>, confirmLabel: verb, tone: operation === "restore" ? "neutral" : "danger" })) return;
    }
    await action("remote", async (signal) => {
      const next = await startStorageTask(token, { node, operation, ...(operation === "inventory" || operation === "preview" ? {} : { planId: result?.planId, confirmed: true }) }, signal);
      setTask(next.task); if (next.task.result) setResult(next.task.result); else if (["inventory", "preview"].includes(operation)) setResult(null);
    });
  };
  const gateText = (gate: string) => ({ missing: txt("需要先生成预览", "Generate a preview first"), blocked: txt("存在保护条件，当前不可清理", "Protected conditions block cleanup"), expired: txt("计划已过期，请重新预览", "Plan expired; generate a new preview"), grace: txt("宽限期内，尚不可执行", "Grace period active; execution unavailable"), ready: txt("计划可执行，服务器会再次校验", "Plan ready; server revalidates before execution") }[gate]);
  const historyGate = cleanupPlanGate(plan, now);
  const remoteGate = cleanupPlanGate(result, now);
  return <div className="storage-governance">
    {element}
    <article className="panel">
      <div className="panel-heading"><div><p className="eyebrow">MANAGER</p><h2>{txt("存储占用与增长", "Storage usage and growth")}</h2></div><button className="ghost" disabled={Boolean(busy)} onClick={() => setRevision((v) => v + 1)}><RefreshCw size={14} />{txt("刷新", "Refresh")}</button></div>
      {error && <div className="alert alert-warning" role="alert">{error}</div>}
      {message && <p role="status">{message}</p>}
      {!inventory ? <p>{busy ? txt("正在读取存储清单…", "Loading storage inventory…") : txt("存储清单暂不可用，请重试。", "Storage inventory unavailable. Please retry.")}</p> : <>
        <div className="governance-summary"><div><Database size={18} /><span>{txt("已测量占用", "Measured usage")}</span><strong>{bytes(inventory.totalKnownBytes)}</strong></div><div><HardDrive size={18} /><span>{txt("统计时间", "Measured at")}</span><strong>{date(inventory.measuredAt)}</strong></div><div><ShieldCheck size={18} /><span>{txt("最近备份", "Latest backup")}</span><strong>{date(inventory.backup?.latestAt)}</strong></div></div>
        <p className="governance-note">{txt("此合计只包含已测量项。未知不代表 0；远端 Builder 与 Registry 需要分别盘点。", "This total includes measured components only. Unknown does not mean zero; remote builders and registries require separate inventory.")}</p>
        <div className="table-wrap"><table><thead><tr><th>{txt("类别 / 位置", "Category / location")}</th><th>{txt("占用", "Usage")}</th><th>{txt("增长", "Growth")}</th><th>{txt("可回收", "Reclaimable")}</th><th>{txt("测量范围", "Measurement scope")}</th></tr></thead><tbody>{inventory.components.map((item) => <tr key={item.id}><td><strong>{storageCategory(item.id, item.label, zh)}</strong><small className="governance-path">{item.location === "manager" ? "Manager" : item.location === "external" ? txt("远端", "Remote") : item.location}</small></td><td>{bytes(item.bytes)}</td><td>{typeof item.growthBytes === "number" && Number.isFinite(item.growthBytes) ? `${item.growthBytes < 0 ? "−" : "+"}${bytes(Math.abs(item.growthBytes))}` : txt("暂无基线", "No baseline")}</td><td>{bytes(item.reclaimableBytes)}</td><td>{item.reason || (item.status === "measured" ? txt("已测量", "Measured") : txt("未测量", "Not measured"))}</td></tr>)}</tbody></table></div>
        <p className="governance-note">{txt("SQLite 空闲页可复用：", "Reusable SQLite pages: ")}{bytes(inventory.databaseReusableBytes)} · {txt("删除历史不会立即缩小数据库文件。", "Deleting history does not immediately shrink the database file.")}</p><p className="governance-note">{inventory.growthSince ? `${txt("增长比较基线：", "Growth baseline: ")}${date(inventory.growthSince)} · ` : ""}{inventory.backup?.note || txt("Manager 数据库需随持久盘备份和迁移；清理不能代替备份。", "Back up and migrate the Manager database with its persistent storage. Cleanup does not replace backups.")}</p>
      </>}
    </article>
    <article className="panel"><div className="panel-heading"><div><p className="eyebrow">RETENTION</p><h2>{txt("历史保留策略", "History retention")}</h2></div></div>
      {policy ? <form onSubmit={(event) => { event.preventDefault(); void action("policy", async (signal) => { const next = await saveStoragePolicy(token, policy, signal); setPolicy(next); setDirty(false); setPlan(null); setMessage(txt("策略已保存。清理前请重新生成预览。", "Policy saved. Generate a fresh preview before cleanup.")); }); }}>
        <div className="governance-policy">{([["summaryDays", txt("构建 / 部署摘要（天）", "Build / deploy summaries (days)")], ["detailDays", txt("详细事件 / 日志（天）", "Detailed events / logs (days)")], ["graceHours", txt("清理宽限期（小时）", "Cleanup grace (hours)")]] as const).map(([key, label]) => <label key={key}>{label}<input type="number" required min={key === "graceHours" ? 24 : key === "summaryDays" ? 7 : 1} max={key === "graceHours" ? 720 : key === "detailDays" ? policy.summaryDays : 3650} step={1} value={policy[key]} onChange={(event) => { setPolicy({ ...policy, [key]: Number(event.target.value) }); setDirty(true); setPlan(null); }} /></label>)}</div>
        <div className="governance-actions"><button type="submit" disabled={Boolean(busy)}>{txt("保存策略", "Save policy")}</button><button type="button" className="ghost" disabled={Boolean(busy) || dirty} onClick={() => void action("preview", async (signal) => setPlan(await previewHistoryCleanup(token, signal)))}>{txt("预览历史清理", "Preview history cleanup")}</button></div>
      {dirty && <p className="governance-note">{txt("有未保存的策略变更；保存后才能生成预览。", "Unsaved policy changes; save before generating a preview.")}</p>}<p className="governance-note">{txt("策略不会自动删除数据，清理需人工预览和确认。Builder 的保留边界由 Agent 单独校验。", "Policies do not delete automatically. Cleanup requires a reviewed preview and confirmation. Builder retention is validated separately by its agent.")}</p></form> : <p className="governance-note">{txt("策略尚不可用。", "Retention policy unavailable.")}</p>}
      {inventory?.historyPlans?.length ? <div className="governance-actions"><label>{txt("重新打开保留的清理计划", "Reopen a saved cleanup plan")}<select value={plan?.planId || ""} disabled={Boolean(busy)} onChange={(event) => { if (event.target.value) void action("plan", async (signal) => setPlan(await getHistoryCleanupPlan(token, event.target.value, signal))); }}><option value="">{txt("选择计划", "Select a plan")}</option>{inventory.historyPlans.filter((item) => item.status === "preview").map((item) => <option key={item.planId} value={item.planId}>{item.planId.slice(0, 12)} · {date(item.eligibleAfter)}</option>)}</select></label></div> : null}
      {plan && <div className="governance-plan"><h3>{txt("已生成清理预览", "Cleanup preview ready")}</h3><p>{txt("候选记录", "Candidate records")} {plan.candidateCount} · {txt("估计可复用空间", "Estimated reusable space")} {bytes(plan.estimatedReclaimableBytes)} · {txt("保护记录", "Protected records")} {plan.protectedCount}</p>{plan.alertHistory && <p>{txt("告警历史", "Alert history")} · {txt("事件实例", "Incidents")} {plan.alertHistory.incidentIds.length} · {txt("通知记录", "Deliveries")} {plan.alertHistory.deliveryIds.length} · {txt("状态事件", "State events")} {plan.alertHistory.eventsCount} · {bytes(plan.alertHistory.estimatedBytes)}{plan.alertHistory.hasMore ? txt(" · 更多历史需另行生成预览", " · More history requires another preview") : ""}</p>}<small>{gateText(historyGate)} · {txt("可执行时间", "Eligible after")} {date(plan.eligibleAfter)} · {txt("计划过期", "Plan expires")} {date(plan.expiresAt)}</small>{plan.blockedReasons?.map((reason) => <p key={reason}>{reason}</p>)}{plan.candidates?.length ? <details><summary>{txt("查看清理对象", "Review cleanup candidates")} ({plan.candidateCount})</summary><div className="table-wrap"><table><thead><tr><th>{txt("应用 / 记录", "App / record")}</th><th>{txt("清理内容", "Cleanup scope")}</th><th>{txt("估计空间", "Estimated size")}</th></tr></thead><tbody>{plan.candidates.slice(0, 200).map((item) => <tr key={`${item.kind}-${item.id}`}><td>{item.app || "—"}<small className="governance-path">{item.id}</small></td><td>{item.action === "summary" ? txt("摘要及详细事件", "Summary and details") : txt("仅详细事件", "Details only")}</td><td>{bytes(item.estimatedBytes)}</td></tr>)}</tbody></table></div>{(plan.truncated || plan.candidates.length > 200) && <p className="governance-note">{txt("此处展示前 200 条；本次清理以服务器保存的候选计划为准，更多数据需另行预览。", "Showing the first 200 records. Cleanup uses the saved candidate plan; remaining data needs a separate preview.")}</p>}</details> : null}<button className="danger" disabled={Boolean(busy) || historyGate !== "ready"} onClick={async () => { if (!await confirm({ title: txt("执行历史清理", "Apply history cleanup"), body: txt(`永久清理此计划中的 ${plan.candidateCount} 条候选记录或其详细事件。同时清理 ${plan.alertHistory?.incidentIds.length || 0} 条告警实例、${plan.alertHistory?.deliveryIds.length || 0} 条通知记录和 ${plan.alertHistory?.eventsCount || 0} 条告警状态事件。运行中记录会由服务器再次检查保护。`, `Permanently clean ${plan.candidateCount} candidate records or their detailed events in this plan. Also clean ${plan.alertHistory?.incidentIds.length || 0} alert incidents, ${plan.alertHistory?.deliveryIds.length || 0} deliveries and ${plan.alertHistory?.eventsCount || 0} alert state events. The server rechecks protection for active records.`) })) return; await action("apply", async (signal) => { const outcome = await executeHistoryCleanup(token, plan.planId, signal); setPlan(null); setMessage(txt(`历史清理：处理 ${outcome.removedCount} 条，跳过 ${outcome.skippedCount} 条；告警实例 ${outcome.alertHistory?.incidentsDeleted || 0} 条，通知记录 ${outcome.alertHistory?.deliveriesDeleted || 0} 条。`, `History cleanup: processed ${outcome.removedCount}, skipped ${outcome.skippedCount}; ${outcome.alertHistory?.incidentsDeleted || 0} alert incidents and ${outcome.alertHistory?.deliveriesDeleted || 0} deliveries deleted.`)); setRevision((v) => v + 1); }); }}>{txt("执行已预览清理", "Apply reviewed cleanup")}</button></div>}
    </article>
    <article className="panel"><div className="panel-heading"><div><p className="eyebrow">BUILDER</p><h2>{txt("远端产物盘点与回收", "Remote artifact inventory and reclamation")}</h2></div></div>
      <p className="governance-note">{txt("盘点在 Builder 后台运行。先预览，再隔离，宽限期后才能永久删除；运行中构建及不完整引用会阻止清理。", "Inventory runs in the builder background. Preview, then quarantine, then delete after the grace period. Running builds and incomplete references block cleanup.")}</p>
      <div className="governance-actions"><label>{txt("Builder 节点", "Builder node")}<select value={node} disabled={taskRunning || Boolean(busy)} onChange={(event) => { setNode(event.target.value); setTask(null); setResult(null); }}><option value="">{txt("选择节点", "Select node")}</option>{inventory?.builders?.map((builder) => <option key={builder.name} value={builder.name}>{builder.name}{builder.status ? ` · ${builder.status}` : ""}</option>)}</select></label><button className="ghost" disabled={!node || Boolean(busy) || taskRunning} onClick={() => void remote("inventory")}>{txt("盘点占用", "Inspect usage")}</button><button disabled={!node || Boolean(busy) || taskRunning} onClick={() => void remote("preview")}>{txt("生成清理预览", "Preview cleanup")}</button></div>
      {inventory?.builderTasks?.some((item) => item.nodeName === node && item.result?.planId) ? <div className="governance-actions"><label>{txt("重新打开 Builder 计划", "Reopen a builder plan")}<select value={task?.id || ""} disabled={Boolean(busy) || taskRunning} onChange={(event) => { const saved = inventory.builderTasks?.find((item) => item.id === event.target.value); if (saved) { setTask(saved); setResult(saved.result || null); } }}><option value="">{txt("选择计划任务", "Select a plan task")}</option>{inventory.builderTasks.filter((item) => item.nodeName === node && item.result?.planId).map((item) => <option key={item.id} value={item.id}>{item.result?.planId?.slice(0, 12)} · {item.result?.operation} · {date(item.updatedAt)}</option>)}</select></label></div> : null}
      {!inventory?.builders?.length && <p className="governance-note">{txt("暂无可盘点的 Builder 节点；注册并更新 Agent 后刷新。", "No builder nodes available. Register and update the agent, then refresh.")}</p>}
      {task && <p role="status">{txt("后台任务", "Background task")} <code>{task.id}</code> · {task.status}{taskRunning ? txt(" · 每 3 秒刷新", " · Refreshing every 3s") : ""}</p>}
      {result && <div className="governance-plan"><p>{txt("总占用", "Total")} {bytes(result.totalBytes)} · {txt("保护占用", "Protected")} {bytes(result.protectedBytes)} · {txt("可回收", "Reclaimable")} {bytes(result.reclaimableBytes)}</p>{result.message && <p>{result.message}</p>}{result.blockedReasons?.map((reason) => <p className="governance-warning" key={reason}>{reason}</p>)}{result.planId && <><code>{result.planId}</code><p className="governance-note">{gateText(remoteGate)} · {txt("计划过期", "Plan expires")} {date(result.expiresAt)} · {txt("可永久删除时间", "Purge after")} {date(result.eligibleAfter)}</p><div className="governance-actions">{result.operation === "preview" && <button disabled={Boolean(busy) || taskRunning || remoteGate !== "ready"} onClick={() => void remote("quarantine")}>{txt("隔离已预览文件", "Quarantine reviewed files")}</button>}{result.operation === "quarantine" && <><button className="ghost" disabled={Boolean(busy) || taskRunning} onClick={() => void remote("restore")}>{txt("恢复隔离文件", "Restore quarantined files")}</button><button className="danger" disabled={Boolean(busy) || taskRunning || remoteGate !== "ready"} onClick={() => void remote("purge")}>{txt("永久删除隔离文件", "Purge quarantined files")}</button></>}</div></>}{result.files?.length ? <details><summary>{txt("文件明细", "File details")} ({result.files.length}{result.fileCount ? ` / ${result.fileCount}` : ""})</summary><div className="table-wrap"><table><thead><tr><th>{txt("路径", "Path")}</th><th>{txt("占用", "Bytes")}</th><th>{txt("状态", "Status")}</th></tr></thead><tbody>{result.files.slice(0, 200).map((file, i) => <tr key={`${file.path}-${i}`}><td><code>{file.path}</code></td><td>{bytes(file.bytes)}</td><td>{file.status}</td></tr>)}</tbody></table></div>{(result.truncated || result.filesTruncated || result.files.length > 200) && <p className="governance-note">{txt("仅显示部分文件；清理严格使用服务器保存的完整计划。", "Partial listing. Cleanup uses the complete server-held plan.")}</p>}</details> : null}</div>}
    </article>
  </div>;
}
