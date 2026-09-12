import { useEffect, useRef, useState } from "react";
import { Input } from "@/components/ui/input";

import { AlertCircle, ChevronDown, Database, HardDrive, RefreshCw, ShieldCheck } from "lucide-react";
import type { Lang } from "../types";
import { executeHistoryCleanup, getHistoryCleanupPlan, getStorageInventory, getStoragePolicy, getStorageTask, previewHistoryCleanup, saveStoragePolicy, startStorageTask, type HistoryCleanupPreview, type StorageInventory, type StorageOperation, type StoragePolicy, type StorageResult, type StorageTask } from "../storageGovernanceApi";
import { cleanupPlanGate, storageBytes, storageCategory, storageTaskFinished, storageTime } from "./storageGovernanceModel";
import { useConfirm } from "./ConfirmDialog";
import { SelectControl } from "./primitives";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

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
  const [busy, setBusy] = useState("load");
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
  const policyFields = [
    { key: "summaryDays", label: txt("构建 / 部署摘要（天）", "Build / deploy summaries (days)"), min: 7, max: 3650 },
    { key: "detailDays", label: txt("详细事件 / 日志（天）", "Detailed events / logs (days)"), min: 1, max: policy?.summaryDays || 3650 },
    { key: "graceHours", label: txt("清理宽限期（小时）", "Cleanup grace (hours)"), min: 24, max: 720 },
  ] as const;
  const invalidPolicyField = (field: typeof policyFields[number]) => !policy || !Number.isInteger(policy[field.key]) || policy[field.key] < field.min || policy[field.key] > field.max;
  const policyInvalid = policyFields.some(invalidPolicyField);
  const applyHistory = async () => {
    if (!plan || !await confirm({ title: txt("执行历史清理", "Apply history cleanup"), body: txt(`永久清理此计划中的 ${plan.candidateCount} 条候选记录或其详细事件。同时清理 ${plan.alertHistory?.incidentIds.length || 0} 条告警实例、${plan.alertHistory?.deliveryIds.length || 0} 条通知记录和 ${plan.alertHistory?.eventsCount || 0} 条告警状态事件。运行中记录会由服务器再次检查保护。`, `Permanently clean ${plan.candidateCount} candidate records or their detailed events in this plan. Also clean ${plan.alertHistory?.incidentIds.length || 0} alert incidents, ${plan.alertHistory?.deliveryIds.length || 0} deliveries and ${plan.alertHistory?.eventsCount || 0} alert state events. The server rechecks protection for active records.`) })) return;
    await action("apply", async (signal) => {
      const outcome = await executeHistoryCleanup(token, plan.planId, signal);
      setPlan(null);
      setMessage(txt(`历史清理：处理 ${outcome.removedCount} 条，跳过 ${outcome.skippedCount} 条；告警实例 ${outcome.alertHistory?.incidentsDeleted || 0} 条，通知记录 ${outcome.alertHistory?.deliveriesDeleted || 0} 条。`, `History cleanup: processed ${outcome.removedCount}, skipped ${outcome.skippedCount}; ${outcome.alertHistory?.incidentsDeleted || 0} alert incidents and ${outcome.alertHistory?.deliveriesDeleted || 0} deliveries deleted.`));
      setRevision((value) => value + 1);
    });
  };
  const loadingPlaceholder = <div className="flex flex-col gap-3" role="status" aria-label={txt("正在读取存储信息", "Loading storage information")}><Skeleton className="h-5 w-1/2" /><Skeleton className="h-5 w-3/4" /><Skeleton className="h-24 w-full" /></div>;

  return <div className="flex min-w-0 flex-col gap-6">
    {element}
    {error ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{txt("操作失败", "Operation failed")}</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}
    {message ? <Alert role="status"><ShieldCheck /><AlertTitle>{txt("操作完成", "Operation completed")}</AlertTitle><AlertDescription>{message}</AlertDescription></Alert> : null}
    <Card aria-busy={busy === "load"}>
      <CardHeader>
        <CardTitle>{txt("存储占用与增长", "Storage usage and growth")}</CardTitle><CardDescription>{txt("Manager 已测量的存储占用、增长与备份记录。", "Measured Manager storage usage, growth, and backup records.")}</CardDescription>
        <CardAction><Button type="button" variant="outline" disabled={Boolean(busy)} onClick={() => setRevision((value) => value + 1)}>{busy === "load" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <RefreshCw data-icon="inline-start" />}{txt("刷新", "Refresh")}</Button></CardAction>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-4">
        {!inventory ? busy === "load" ? loadingPlaceholder : <Empty><EmptyHeader><EmptyMedia variant="icon"><Database /></EmptyMedia><EmptyTitle>{txt("存储清单暂不可用", "Storage inventory unavailable")}</EmptyTitle><EmptyDescription>{txt("请刷新后重试。", "Refresh to try again.")}</EmptyDescription></EmptyHeader></Empty> : <>
          <dl className="grid gap-4 md:grid-cols-3">
            <div className="flex min-w-0 flex-col gap-2"><dt className="text-sm text-muted-foreground">{txt("已测量占用", "Measured usage")}</dt><dd>{bytes(inventory.totalKnownBytes)}</dd></div>
            <div className="flex min-w-0 flex-col gap-2"><dt className="text-sm text-muted-foreground">{txt("统计时间", "Measured at")}</dt><dd>{date(inventory.measuredAt)}</dd></div>
            <div className="flex min-w-0 flex-col gap-2"><dt className="text-sm text-muted-foreground">{txt("最近备份", "Latest backup")}</dt><dd>{date(inventory.backup?.latestAt)}</dd></div>
          </dl>
          <p className="text-sm text-muted-foreground">{txt("此合计只包含已测量项。未知不代表 0；远端 Builder 与 Registry 需要分别盘点。", "This total includes measured components only. Unknown does not mean zero; remote builders and registries require separate inventory.")}</p>
          {inventory.components.length ? <Table containerProps={{ tabIndex: 0, role: "region", "aria-label": txt("存储占用，可横向滚动", "Storage usage, horizontally scrollable") }} className="min-w-[720px]">
            <TableHeader><TableRow><TableHead>{txt("类别 / 位置", "Category / location")}</TableHead><TableHead>{txt("占用", "Usage")}</TableHead><TableHead>{txt("增长", "Growth")}</TableHead><TableHead>{txt("可回收", "Reclaimable")}</TableHead><TableHead>{txt("测量范围", "Measurement scope")}</TableHead></TableRow></TableHeader>
            <TableBody>{inventory.components.map((item) => <TableRow key={item.id}>
              <TableCell><span className="flex flex-col gap-1"><span>{storageCategory(item.id, item.label, zh)}</span><span className="text-xs text-muted-foreground">{item.location === "manager" ? "Manager" : item.location === "external" ? txt("远端", "Remote") : item.location}</span></span></TableCell>
              <TableCell>{bytes(item.bytes)}</TableCell><TableCell>{typeof item.growthBytes === "number" && Number.isFinite(item.growthBytes) ? `${item.growthBytes < 0 ? "−" : "+"}${bytes(Math.abs(item.growthBytes))}` : txt("暂无基线", "No baseline")}</TableCell><TableCell>{bytes(item.reclaimableBytes)}</TableCell><TableCell className="max-w-80 whitespace-normal break-words">{item.reason || <Badge variant="outline">{item.status === "measured" ? txt("已测量", "Measured") : txt("未测量", "Not measured")}</Badge>}</TableCell>
            </TableRow>)}</TableBody>
          </Table> : <Empty><EmptyHeader><EmptyTitle>{txt("暂无测量项", "No measured components")}</EmptyTitle></EmptyHeader></Empty>}
          <p className="text-sm text-muted-foreground">{txt("SQLite 空闲页可复用：", "Reusable SQLite pages: ")}{bytes(inventory.databaseReusableBytes)} · {txt("删除历史不会立即缩小数据库文件。", "Deleting history does not immediately shrink the database file.")}</p>
          <p className="text-sm text-muted-foreground">{inventory.growthSince ? `${txt("增长比较基线：", "Growth baseline: ")}${date(inventory.growthSince)} · ` : ""}{inventory.backup?.note || txt("Manager 数据库需随持久盘备份和迁移；清理不能代替备份。", "Back up and migrate the Manager database with its persistent storage. Cleanup does not replace backups.")}</p>
        </>}
      </CardContent>
    </Card>

    <Card>
      <CardHeader><CardTitle>{txt("历史保留策略", "History retention")}</CardTitle><CardDescription>{txt("设置构建、部署与告警历史的保留期限。", "Set retention for build, deployment, and alert history.")}</CardDescription></CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-6">
        {policy ? <form id="storage-policy-form" className="flex flex-col gap-4" onSubmit={(event) => { event.preventDefault(); if (policyInvalid) return; void action("policy", async (signal) => { const next = await saveStoragePolicy(token, policy, signal); setPolicy(next); setDirty(false); setPlan(null); setMessage(txt("策略已保存。清理前请重新生成预览。", "Policy saved. Generate a fresh preview before cleanup.")); }); }}>
          <FieldGroup className="grid gap-4 md:grid-cols-3">
            {policyFields.map((field) => <Field key={field.key} data-disabled={Boolean(busy)} data-invalid={invalidPolicyField(field)}><FieldLabel htmlFor={`storage-policy-${field.key}`}>{field.label}</FieldLabel><Input id={`storage-policy-${field.key}`} type="number" required disabled={Boolean(busy)} min={field.min} max={field.max} step={1} value={policy[field.key]} aria-invalid={invalidPolicyField(field)} aria-describedby={invalidPolicyField(field) ? `storage-policy-${field.key}-error` : undefined} onChange={(event) => { setPolicy({ ...policy, [field.key]: Number(event.target.value) }); setDirty(true); setPlan(null); }} />{invalidPolicyField(field) ? <FieldError id={`storage-policy-${field.key}-error`}>{txt(`请输入 ${field.min}–${field.max} 之间的整数。`, `Enter an integer between ${field.min} and ${field.max}.`)}</FieldError> : null}</Field>)}
          </FieldGroup>
          {dirty ? <Alert><AlertCircle /><AlertTitle>{txt("策略尚未保存", "Unsaved policy changes")}</AlertTitle><AlertDescription>{txt("保存后才能生成新的清理预览。", "Save before generating a new cleanup preview.")}</AlertDescription></Alert> : null}
          <FieldDescription>{txt("策略不会自动删除数据，清理需人工预览和确认。Builder 的保留边界由 Agent 单独校验。", "Policies do not delete automatically. Cleanup requires a reviewed preview and confirmation. Builder retention is validated separately by its agent.")}</FieldDescription>
          <div className="flex flex-wrap gap-2"><Button type="submit" disabled={Boolean(busy) || policyInvalid}>{busy === "policy" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}{txt("保存策略", "Save policy")}</Button><Button variant="outline" type="button" disabled={Boolean(busy) || dirty} onClick={() => void action("preview", async (signal) => setPlan(await previewHistoryCleanup(token, signal)))}>{busy === "preview" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}{txt("预览历史清理", "Preview history cleanup")}</Button></div>
        </form> : busy === "load" ? loadingPlaceholder : <Empty><EmptyHeader><EmptyTitle>{txt("策略尚不可用", "Retention policy unavailable")}</EmptyTitle></EmptyHeader></Empty>}
        {inventory?.historyPlans?.some((item) => item.status === "preview") ? <FieldGroup><Field className="max-w-md" data-disabled={Boolean(busy)}><FieldLabel htmlFor="storage-history-plan">{txt("重新打开保留的清理计划", "Reopen a saved cleanup plan")}</FieldLabel><SelectControl id="storage-history-plan" ariaLabel={txt("重新打开保留的清理计划", "Reopen a saved cleanup plan")} disabled={Boolean(busy)} value={plan?.planId || ""} onChange={(value) => { if (value) void action("plan", async (signal) => setPlan(await getHistoryCleanupPlan(token, value, signal))); }} options={[{ value: "", label: txt("选择计划", "Select a plan") }, ...inventory.historyPlans.filter((item) => item.status === "preview").map((item) => ({ value: item.planId, label: `${item.planId.slice(0, 12)} · ${date(item.eligibleAfter)}` }))]} /></Field></FieldGroup> : null}
        {plan ? <>
          <Separator />
          <Card size="sm">
            <CardHeader><CardTitle>{txt("已生成清理预览", "Cleanup preview ready")}</CardTitle><CardDescription><code className="break-all">{plan.planId}</code></CardDescription></CardHeader>
            <CardContent className="flex min-w-0 flex-col gap-4">
              <dl className="grid gap-4 md:grid-cols-3"><div className="flex flex-col gap-1"><dt className="text-sm text-muted-foreground">{txt("候选记录", "Candidate records")}</dt><dd>{plan.candidateCount}</dd></div><div className="flex flex-col gap-1"><dt className="text-sm text-muted-foreground">{txt("估计可复用空间", "Estimated reusable space")}</dt><dd>{bytes(plan.estimatedReclaimableBytes)}</dd></div><div className="flex flex-col gap-1"><dt className="text-sm text-muted-foreground">{txt("保护记录", "Protected records")}</dt><dd>{plan.protectedCount}</dd></div></dl>
              {plan.alertHistory ? <p className="text-sm text-muted-foreground">{txt("告警历史", "Alert history")} · {txt("事件实例", "Incidents")} {plan.alertHistory.incidentIds.length} · {txt("通知记录", "Deliveries")} {plan.alertHistory.deliveryIds.length} · {txt("状态事件", "State events")} {plan.alertHistory.eventsCount} · {bytes(plan.alertHistory.estimatedBytes)}{plan.alertHistory.hasMore ? txt(" · 更多历史需另行生成预览", " · More history requires another preview") : ""}</p> : null}
              <Alert variant={historyGate === "blocked" ? "destructive" : "default"}><AlertCircle /><AlertTitle>{gateText(historyGate)}</AlertTitle><AlertDescription><p>{txt("可执行时间", "Eligible after")} {date(plan.eligibleAfter)}</p><p>{txt("计划过期", "Plan expires")} {date(plan.expiresAt)}</p>{plan.blockedReasons?.map((reason) => <p key={reason}>{reason}</p>)}</AlertDescription></Alert>
              {plan.candidates?.length ? <Collapsible className="flex min-w-0 flex-col gap-3"><CollapsibleTrigger render={<Button type="button" variant="outline" className="self-start" />}><ChevronDown data-icon="inline-start" />{txt("查看清理对象", "Review cleanup candidates")} ({plan.candidateCount})</CollapsibleTrigger><CollapsibleContent className="flex min-w-0 flex-col gap-3"><Table containerProps={{ tabIndex: 0, role: "region", "aria-label": txt("历史清理对象，可横向滚动", "History cleanup candidates, horizontally scrollable") }} className="min-w-[560px]"><TableHeader><TableRow><TableHead>{txt("应用 / 记录", "App / record")}</TableHead><TableHead>{txt("清理内容", "Cleanup scope")}</TableHead><TableHead>{txt("估计空间", "Estimated size")}</TableHead></TableRow></TableHeader><TableBody>{plan.candidates.slice(0, 200).map((item) => <TableRow key={`${item.kind}-${item.id}`}><TableCell><span className="flex flex-col gap-1"><span>{item.app || "—"}</span><code className="text-xs text-muted-foreground">{item.id}</code></span></TableCell><TableCell>{item.action === "summary" ? txt("摘要及详细事件", "Summary and details") : txt("仅详细事件", "Details only")}</TableCell><TableCell>{bytes(item.estimatedBytes)}</TableCell></TableRow>)}</TableBody></Table>{plan.truncated || plan.candidates.length > 200 ? <p className="text-sm text-muted-foreground">{txt("此处展示前 200 条；本次清理以服务器保存的候选计划为准，更多数据需另行预览。", "Showing the first 200 records. Cleanup uses the saved candidate plan; remaining data needs a separate preview.")}</p> : null}</CollapsibleContent></Collapsible> : <Empty><EmptyHeader><EmptyTitle>{txt("没有需要清理的历史记录", "No history records to clean")}</EmptyTitle></EmptyHeader></Empty>}
            </CardContent>
            <CardFooter><Button type="button" variant="destructive" disabled={Boolean(busy) || historyGate !== "ready"} onClick={() => void applyHistory()}>{busy === "apply" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}{txt("执行已预览清理", "Apply reviewed cleanup")}</Button></CardFooter>
          </Card>
        </> : null}
      </CardContent>
    </Card>

    <Card aria-busy={taskRunning || busy === "remote"}>
      <CardHeader><CardTitle>{txt("远端产物盘点与回收", "Remote artifact inventory and reclamation")}</CardTitle><CardDescription>{txt("盘点在 Builder 后台运行。先预览，再隔离，宽限期后才能永久删除；运行中构建及不完整引用会阻止清理。", "Inventory runs in the builder background. Preview, then quarantine, then delete after the grace period. Running builds and incomplete references block cleanup.")}</CardDescription></CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-6">
        <FieldGroup><Field className="max-w-md" data-disabled={taskRunning || Boolean(busy) || !inventory?.builders?.length}><FieldLabel htmlFor="storage-builder-node">{txt("Builder 节点", "Builder node")}</FieldLabel><SelectControl id="storage-builder-node" ariaLabel={txt("Builder 节点", "Builder node")} disabled={taskRunning || Boolean(busy) || !inventory?.builders?.length} value={node} onChange={(value) => { setNode(value); setTask(null); setResult(null); }} options={[{ value: "", label: txt("选择节点", "Select node") }, ...(inventory?.builders || []).map((builder) => ({ value: builder.name, label: builder.status ? `${builder.name} · ${builder.status}` : builder.name }))]} /></Field></FieldGroup>
        <div className="flex flex-wrap gap-2"><Button type="button" variant="outline" disabled={!node || Boolean(busy) || taskRunning} onClick={() => void remote("inventory")}>{txt("盘点占用", "Inspect usage")}</Button><Button type="button" disabled={!node || Boolean(busy) || taskRunning} onClick={() => void remote("preview")}>{txt("生成清理预览", "Preview cleanup")}</Button></div>
        {inventory?.builderTasks?.some((item) => item.nodeName === node && item.result?.planId) ? <FieldGroup><Field className="max-w-md" data-disabled={Boolean(busy) || taskRunning}><FieldLabel htmlFor="storage-builder-plan">{txt("重新打开 Builder 计划", "Reopen a builder plan")}</FieldLabel><SelectControl id="storage-builder-plan" ariaLabel={txt("重新打开 Builder 计划", "Reopen a builder plan")} disabled={Boolean(busy) || taskRunning} value={task?.id || ""} onChange={(value) => { const saved = inventory.builderTasks?.find((item) => item.id === value); if (saved) { setTask(saved); setResult(saved.result || null); } }} options={[{ value: "", label: txt("选择计划任务", "Select a plan task") }, ...inventory.builderTasks.filter((item) => item.nodeName === node && item.result?.planId).map((item) => ({ value: item.id, label: `${item.result?.planId?.slice(0, 12)} · ${item.result?.operation} · ${date(item.updatedAt)}` }))]} /></Field></FieldGroup> : null}
        {busy === "load" && !inventory ? loadingPlaceholder : !inventory?.builders?.length ? <Empty><EmptyHeader><EmptyMedia variant="icon"><HardDrive /></EmptyMedia><EmptyTitle>{txt("暂无可盘点的 Builder 节点", "No builder nodes available")}</EmptyTitle><EmptyDescription>{txt("注册并更新 Agent 后刷新。", "Register and update the agent, then refresh.")}</EmptyDescription></EmptyHeader></Empty> : null}
        {busy === "remote" ? <Alert role="status"><Spinner aria-hidden="true" /><AlertTitle>{txt("正在提交操作", "Submitting operation")}</AlertTitle></Alert> : null}
        {task ? <Alert role="status">{taskRunning ? <Spinner aria-hidden="true" /> : <HardDrive />}<AlertTitle className="flex flex-wrap items-center gap-2">{txt("后台任务", "Background task")}<Badge variant={taskRunning ? "secondary" : ["failed", "error", "cancelled", "expired"].includes(task.status) ? "destructive" : "outline"}>{task.status}</Badge></AlertTitle><AlertDescription><code className="break-all">{task.id}</code>{taskRunning ? <p>{txt("正在自动刷新任务状态。", "Task status refreshes automatically.")}</p> : null}</AlertDescription></Alert> : null}
        {result ? <Card size="sm">
          <CardHeader><CardTitle>{txt("盘点结果", "Inventory result")}</CardTitle>{result.planId ? <CardDescription><code className="break-all">{result.planId}</code></CardDescription> : null}</CardHeader>
          <CardContent className="flex min-w-0 flex-col gap-4">
            <dl className="grid gap-4 md:grid-cols-3"><div className="flex flex-col gap-1"><dt className="text-sm text-muted-foreground">{txt("总占用", "Total")}</dt><dd>{bytes(result.totalBytes)}</dd></div><div className="flex flex-col gap-1"><dt className="text-sm text-muted-foreground">{txt("保护占用", "Protected")}</dt><dd>{bytes(result.protectedBytes)}</dd></div><div className="flex flex-col gap-1"><dt className="text-sm text-muted-foreground">{txt("可回收", "Reclaimable")}</dt><dd>{bytes(result.reclaimableBytes)}</dd></div></dl>
            {result.message ? <p className="text-sm text-muted-foreground">{result.message}</p> : null}
            {result.planId || result.blockedReasons?.length ? <Alert variant={remoteGate === "blocked" ? "destructive" : "default"}><AlertCircle /><AlertTitle>{gateText(remoteGate)}</AlertTitle><AlertDescription>{result.planId ? <><p>{txt("计划过期", "Plan expires")} {date(result.expiresAt)}</p><p>{txt("可永久删除时间", "Purge after")} {date(result.eligibleAfter)}</p></> : null}{result.blockedReasons?.map((reason) => <p key={reason}>{reason}</p>)}</AlertDescription></Alert> : null}
            {result.files?.length ? <Collapsible className="flex min-w-0 flex-col gap-3"><CollapsibleTrigger render={<Button type="button" variant="outline" className="self-start" />}><ChevronDown data-icon="inline-start" />{txt("文件明细", "File details")} ({result.files.length}{result.fileCount ? ` / ${result.fileCount}` : ""})</CollapsibleTrigger><CollapsibleContent className="flex min-w-0 flex-col gap-3"><Table containerProps={{ tabIndex: 0, role: "region", "aria-label": txt("产物文件明细，可横向滚动", "Artifact files, horizontally scrollable") }} className="min-w-[560px]"><TableHeader><TableRow><TableHead>{txt("路径", "Path")}</TableHead><TableHead>{txt("占用", "Bytes")}</TableHead><TableHead>{txt("状态", "Status")}</TableHead></TableRow></TableHeader><TableBody>{result.files.slice(0, 200).map((file, index) => <TableRow key={`${file.path}-${index}`}><TableCell><code>{file.path}</code></TableCell><TableCell>{bytes(file.bytes)}</TableCell><TableCell><Badge variant="outline">{file.status}</Badge></TableCell></TableRow>)}</TableBody></Table>{result.truncated || result.filesTruncated || result.files.length > 200 ? <p className="text-sm text-muted-foreground">{txt("仅显示部分文件；清理严格使用服务器保存的完整计划。", "Partial listing. Cleanup uses the complete server-held plan.")}</p> : null}</CollapsibleContent></Collapsible> : null}
          </CardContent>
          {result.planId && (result.operation === "preview" || result.operation === "quarantine") ? <CardFooter className="flex-wrap gap-2">{result.operation === "preview" ? <Button type="button" disabled={Boolean(busy) || taskRunning || remoteGate !== "ready"} onClick={() => void remote("quarantine")}>{txt("隔离已预览文件", "Quarantine reviewed files")}</Button> : <><Button type="button" variant="outline" disabled={Boolean(busy) || taskRunning} onClick={() => void remote("restore")}>{txt("恢复隔离文件", "Restore quarantined files")}</Button><Button type="button" variant="destructive" disabled={Boolean(busy) || taskRunning || remoteGate !== "ready"} onClick={() => void remote("purge")}>{txt("永久删除隔离文件", "Purge quarantined files")}</Button></>}</CardFooter> : null}
        </Card> : null}
      </CardContent>
    </Card>
  </div>;
}
