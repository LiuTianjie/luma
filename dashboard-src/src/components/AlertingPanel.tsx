import { fetchAlertTab } from "../alertingApi";
import { useCallback, useEffect, useRef, useState } from "react";
import { Input } from "@/components/ui/input";

import type { FormEvent } from "react";
import type { Lang } from "../types";
import { alertEventLabel, localizeAlertPreset, alertStatusLabel, channelRequest, deleteAlerting, getAlerting, mergeAlertPages, newAlertRule, postAlerting, ruleRequest } from "../alertingApi";
import type { AlertChannel, AlertDelivery, AlertEvent, AlertIncident, AlertOverview, AlertPage, AlertPreset, AlertRule, AlertTab } from "../alertingApi";
import "./AlertingPanel.css";
import { useRouter } from "../router";
import { SelectControl } from "./primitives";
import { AlertCircle } from "lucide-react";
import { Alert, AlertAction, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Field, FieldLabel } from "@/components/ui/field";

function stamp(value: number | null | undefined, zh: boolean) { return value ? new Date(value * 1000).toLocaleString(zh ? "zh-CN" : "en-US", { hour12: false }) : "—"; }
function Badge({ status, zh }: { status: string; zh: boolean }) { return <span className={`alert-badge ${status}`}>{alertStatusLabel(status, zh)}</span>; }

export function formatAlertObservedValue(metric: string, value: number | null | undefined, noData: boolean | undefined, zh: boolean): string {
  if (noData || typeof value !== "number" || !Number.isFinite(value)) return zh ? "缺少采样" : "No data";
  if (metric === "build.failed") {
    if (value === 1) return zh ? "失败" : "Failed";
    if (value === 0) return zh ? "未失败" : "Not failed";
  }
  const formatted = new Intl.NumberFormat(zh ? "zh-CN" : "en-US", { maximumFractionDigits: 1 }).format(value);
  if (metric === "node.offline" || metric === "task.queue_age" || metric === "app.http_p95") return `${formatted} ${zh ? "秒" : "s"}`;
  if (["node.cpu", "node.memory", "node.disk", "node.inode"].includes(metric)) return `${formatted}%`;
  if (metric === "app.http_5xx_ratio") return `${new Intl.NumberFormat(zh ? "zh-CN" : "en-US", { style: "percent", maximumFractionDigits: 1 }).format(value)}`;
  return formatted;
}

export function AlertingPanel({ lang, token, tab, nodeNames = [], applicationNames = [] }: { lang: Lang; token: string; tab: AlertTab; nodeNames?: string[]; applicationNames?: string[] }) {
  const zh = lang === "zh";
  const { path, navigate } = useRouter();
  const routeId = (() => { try { return decodeURIComponent(path.split("/")[3] || ""); } catch { return path.split("/")[3] || ""; } })();
  const basePath = tab === "rules" ? "/observe/rules" : tab === "notifications" ? "/observe/channels" : "/observe";
  const editor = Boolean(routeId);
  const initializedRoute = useRef("");
  const [overview, setOverview] = useState<AlertOverview>();
  const [presets, setPresets] = useState<AlertPreset[]>([]);
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [channels, setChannels] = useState<AlertChannel[]>([]);
  const [incidents, setIncidents] = useState<AlertPage<AlertIncident>>({ items: [] });
  const [deliveries, setDeliveries] = useState<AlertPage<AlertDelivery>>({ items: [] });
  const [filter, setFilter] = useState("firing");
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [rule, setRule] = useState<AlertRule | null>(null);
  const [channel, setChannel] = useState<AlertChannel | null>(null);
  const [secret, setSecret] = useState("");
  const [detail, setDetail] = useState<{ incident: AlertIncident; events: AlertEvent[] }>();
  const [refresh, setRefresh] = useState(0);
  useEffect(() => { const reload = () => { setExpanded(false); setRefresh((n) => n + 1); }; window.addEventListener("luma:refresh", reload); return () => window.removeEventListener("luma:refresh", reload); }, []);
  const [expanded, setExpanded] = useState(false);
  const mounted = useRef(true);
  const actionController = useRef<AbortController | null>(null);
  const scope = useRef(0);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; actionController.current?.abort(); }; }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    let controller: AbortController;
    const load = async () => {
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 15000);
      try {
        const next = await fetchAlertTab(tab, token, filter, controller.signal);
        if (cancelled) return;
        setOverview(next.overview);
        if (next.presets) setPresets(next.presets.items.map((preset) => localizeAlertPreset(preset, zh)));
        if (next.rules) setRules(next.rules.items);
        if (next.channels) setChannels(next.channels.items);
        // Expanded history remains a browsing snapshot until explicit refresh.
        if (!expanded && next.incidents) setIncidents(next.incidents);
        if (!expanded && next.deliveries) setDeliveries(next.deliveries);
        setLoaded(true); setError("");
      } catch (err) { if (!cancelled) setError(err instanceof Error ? err.message : String(err)); }
      finally { window.clearTimeout(timeout); if (!cancelled) timer = window.setTimeout(() => void load(), 15000); }
    };
    void load();
    return () => { cancelled = true; controller?.abort(); window.clearTimeout(timer); };
  }, [token, tab, filter, refresh, expanded, zh]);

  useEffect(() => { scope.current += 1; actionController.current?.abort(); setDetail(undefined); setActionError(""); setRule(null); setChannel(null); setSecret(""); }, [tab, token, filter, path]);
  useEffect(() => {
    if (!loaded || initializedRoute.current === path) return;
    initializedRoute.current = path;
    setActionError(""); setRule(null); setChannel(null); setSecret("");
    if (!routeId) return;
    if (tab === "rules") {
      const item = routeId === "new" ? (presets[0] ? newAlertRule(presets[0]) : null) : rules.find((item) => item.id === routeId);
      if (item) setRule({ ...item, channelIds: [...item.channelIds] });
      else setActionError(zh ? "规则不存在或已删除。" : "Rule not found.");
    }
    if (tab === "notifications") {
      const item = routeId === "new" ? { id: "", name: zh ? "飞书告警" : "Feishu alerts", type: "feishu" as const, enabled: true, appId: "", chatId: "", appSecretConfigured: false } : channels.find((item) => item.id === routeId);
      if (item) setChannel({ ...item }); else setActionError(zh ? "渠道不存在或已删除。" : "Channel not found.");
    }
  }, [loaded, path, routeId, tab, presets, rules, channels, zh]);
  useEffect(() => {
    if (tab !== "alerts" || !routeId) return;
    const controller = new AbortController(); let cancelled = false;
    setDetail(undefined);
    void getAlerting<{ incident: AlertIncident; events: AlertEvent[] }>(`incidents/${encodeURIComponent(routeId)}`, token, controller.signal).then((result) => { if (!cancelled) setDetail(result); }).catch((err) => { if (!cancelled) setActionError(String(err)); });
    return () => { cancelled = true; controller.abort(); };
  }, [tab, routeId, token, refresh]);
  const act = useCallback(async (action: (signal: AbortSignal) => Promise<unknown>, success?: string, refreshAfter = true) => {
    if (busy) return;
    const currentScope = scope.current;
    const controller = new AbortController(); actionController.current = controller;
    setBusy(true); setActionError(""); setNotice("");
    const timeout = window.setTimeout(() => controller.abort(), 20000);
    try {
      await action(controller.signal);
      if (mounted.current && currentScope === scope.current) { if (success) setNotice(success); if (refreshAfter) { setExpanded(false); setRefresh((n) => n + 1); } }
    } catch (err) { if (mounted.current && currentScope === scope.current) setActionError(err instanceof Error ? err.message : String(err)); }
    finally { window.clearTimeout(timeout); if (mounted.current) setBusy(false); }
  }, [busy]);
  function editChannel(item: AlertChannel) { navigate(`/observe/channels/${encodeURIComponent(item.id || "new")}`); }
  const silenceActive = (overview?.silencedUntil || 0) > Date.now() / 1000;
  const channelNames = (ids: string[]) => ids.map((id) => channels.find((c) => c.id === id)?.name || id).join(", ");
  const refreshNow = () => { setExpanded(false); setRefresh((n) => n + 1); };
  function saveRule(event: FormEvent) { event.preventDefault(); if (!rule) return; void act(async (signal) => { await postAlerting("rules", token, ruleRequest(rule), signal); if (signal.aborted) return; setRule(null); setRefresh((n) => n + 1); navigate(basePath); }, zh ? "规则已保存" : "Rule saved"); }
  function saveChannel(event: FormEvent) { event.preventDefault(); if (!channel) return; void act(async (signal) => { await postAlerting("channels", token, channelRequest(channel.id, channel.name, channel.enabled, channel.appId, secret, channel.chatId), signal); if (signal.aborted) return; setChannel(null); setSecret(""); setRefresh((n) => n + 1); navigate(basePath); }, zh ? "渠道已保存，尚未发送测试消息" : "Channel saved; no test message has been sent"); }

  return <div className={`alerting-panel${editor ? " is-editor" : ""}`}>
    {editor ? <Button type="button" variant="outline" size="sm" className="mb-3 w-fit" onClick={() => navigate(basePath)}>{zh ? "返回列表" : "Back to list"}</Button> : null}

    {error ? (
      <Alert variant="destructive" className="mb-4">
        <AlertCircle />
        <AlertTitle>{loaded ? (zh ? "刷新失败" : "Refresh failed") : (zh ? "告警服务读取失败" : "Unable to load alerting")}</AlertTitle>
        <AlertDescription>{loaded ? (zh ? "以下为上次成功读取的数据。" : "Showing previously loaded data.") : null} {error}</AlertDescription>
        <AlertAction>
          <Button type="button" variant="outline" size="sm" onClick={refreshNow}>{zh ? "重试" : "Retry"}</Button>
        </AlertAction>
      </Alert>
    ) : null}
    {actionError ? (
      <Alert variant="destructive" className="mb-4">
        <AlertCircle />
        <AlertTitle>{zh ? "操作失败" : "Action failed"}</AlertTitle>
        <AlertDescription>{actionError}</AlertDescription>
      </Alert>
    ) : null}
    {notice ? (
      <Alert className="mb-4">
        <AlertDescription>{notice}</AlertDescription>
      </Alert>
    ) : null}
    {!loaded && !error && <div className="panel alert-empty">{zh ? "正在读取告警配置与状态…" : "Loading alert configuration and status…"}</div>}
    {loaded && overview && <>
      {!editor && tab === "alerts" && <section className="panel">
        <div className="alert-toolbar"><h2>{zh ? "告警引擎" : "Alert engine"}</h2><p className="text-sm text-muted-foreground">{zh ? "最近检查" : "Last evaluation"} · {stamp(overview.lastEvaluatedAt, zh)}</p></div>
        <div className="alert-summary"><span><strong>{overview.counts.firing}</strong>{zh ? "触发中" : "firing"}</span><span><strong>{overview.counts.pending}</strong>{zh ? "等待持续条件" : "pending duration"}</span><span><strong>{overview.enabledRules}</strong>{zh ? "启用规则" : "enabled rules"}</span></div>
        {!overview.lastEvaluatedAt && <p className="alert-muted">{zh ? "尚未完成后台检查，请留意后续刷新。" : "No background evaluation has completed yet."}</p>}
        {overview.lastEvaluatedAt && Date.now() / 1000 - overview.lastEvaluatedAt > 120 ? (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>{zh ? "检查滞后" : "Evaluation stale"}</AlertTitle>
            <AlertDescription>{zh ? "后台检查已超过 2 分钟未更新，请检查 Manager。" : "No evaluation for over 2 minutes. Check the Manager."}</AlertDescription>
          </Alert>
        ) : null}
        {tab === "alerts" && <div className="flex flex-wrap items-center gap-3">
          <span className="text-sm">{silenceActive ? `${zh ? "通知静默至" : "Notifications silenced until"} ${stamp(overview.silencedUntil, zh)}` : (zh ? "通知未静默" : "Notifications are not silenced")}</span>
          <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void act((signal) => postAlerting("silence", token, { seconds: silenceActive ? 0 : 3600 }, signal), zh ? "静默设置已更新" : "Silence updated")}>{silenceActive ? (zh ? "解除静默" : "End silence") : (zh ? "维护静默 1 小时" : "Silence for 1 hour")}</Button>
          <p className="basis-full text-sm text-muted-foreground">{zh ? "静默期间继续记录异常，暂停告警通知；手动测试仍会发送。" : "Evaluation continues while alert notifications are silenced. Manual tests still send."}</p>
        </div>}
      </section>}
      {silenceActive && tab !== "alerts" ? (
        <Alert className="mb-4">
          <AlertTitle>{zh ? "通知已静默" : "Notifications silenced"}</AlertTitle>
          <AlertDescription>{zh ? "告警通知正在维护静默（手动测试消息仍会发送），截至 " : "Alert notifications are silenced; manual tests still send. Silence ends at "}{stamp(overview.silencedUntil, zh)}</AlertDescription>
        </Alert>
      ) : null}
      {tab === "alerts" && <section className="panel">
        {!editor && <><div className="alert-toolbar"><h2>{zh ? "告警与历史" : "Incidents and history"}</h2><Field className="w-52">
          <FieldLabel>{zh ? "状态" : "Status"}</FieldLabel>
          <SelectControl
            className="min-w-0"
            value={filter}
            onChange={(value) => { setExpanded(false); setIncidents({ items: [] }); setFilter(value); }}
            options={[
              { value: "firing", label: zh ? "触发中" : "Firing" },
              { value: "pending", label: zh ? "等待持续条件" : "Pending duration" },
              { value: "resolved", label: zh ? "已恢复" : "Resolved" },
              { value: "closed", label: zh ? "管理关闭" : "Closed by configuration" },
              { value: "", label: zh ? "全部历史" : "All history" },
            ]}
          />
        </Field></div>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{zh ? "规则 / 对象" : "Rule / target"}</TableHead>
              <TableHead>{zh ? "状态" : "State"}</TableHead>
              <TableHead>{zh ? "观测值" : "Observed"}</TableHead>
              <TableHead>{zh ? "开始 / 结束" : "Started / ended"}</TableHead>
              <TableHead className="text-right">{zh ? "操作" : "Actions"}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {incidents.items.map((item) => (
              <TableRow key={item.id}>
                <TableCell>
                  <div className="font-medium">{item.ruleName}</div>
                  <div className="text-sm text-muted-foreground">{item.target}</div>
                  <div className="text-sm text-muted-foreground">{item.severity === "critical" ? (zh ? "严重" : "Critical") : (zh ? "警告" : "Warning")}</div>
                </TableCell>
                <TableCell>
                  <Badge status={item.status} zh={zh} />
                  {item.acknowledgedAt ? <div className="mt-1 text-sm text-muted-foreground">{zh ? "已确认" : "Acknowledged"} · {stamp(item.acknowledgedAt, zh)}</div> : null}
                </TableCell>
                <TableCell>{formatAlertObservedValue(item.metric, item.value, item.noData, zh)}</TableCell>
                <TableCell>
                  <div>{stamp(item.startedAt, zh)}</div>
                  {item.resolvedAt ? <div className="text-sm text-muted-foreground">{stamp(item.resolvedAt, zh)}</div> : null}
                </TableCell>
                <TableCell className="text-right">
                  <div className="flex justify-end gap-2">
                    <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => navigate(`/observe/incidents/${encodeURIComponent(item.id)}`)}>{zh ? "时间线" : "Timeline"}</Button>
                    {!item.acknowledgedAt && !(["resolved", "closed"].includes(item.status)) ? (
                      <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void act((signal) => postAlerting(`incidents/${encodeURIComponent(item.id)}/ack`, token, {}, signal), zh ? "已确认；异常恢复前仍保留记录" : "Acknowledged; incident remains until recovery")}>{zh ? "确认" : "Acknowledge"}</Button>
                    ) : null}
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {!incidents.items.length && <div className="alert-empty">{zh ? "当前筛选下暂无告警。先在规则中配置需要关注的条件。" : "No incidents match this filter. Configure rules for the conditions you want to monitor."}</div>}
        {incidents.nextCursor && <Button variant="outline" disabled={busy} onClick={() => void act(async (signal) => { const page = await getAlerting<AlertPage<AlertIncident>>(`incidents?limit=50&cursor=${encodeURIComponent(incidents.nextCursor!)}${filter ? `&status=${filter}` : ""}`, token, signal); setIncidents((old) => ({ ...page, items: mergeAlertPages(old.items, page.items) })); setExpanded(true); }, undefined, false)}>{zh ? "加载更多历史" : "Load more history"}</Button>}
        {expanded && <small>{zh ? "分页浏览中，刷新可回到最新记录。" : "Browsing history. Refresh to return to the latest records."}</small>}
        </>}
        {editor && !detail && !actionError && <p>{zh ? "正在读取事件…" : "Loading incident…"}</p>}
        {detail && <div className="alert-details"><div className="alert-toolbar"><h3>{detail.incident.ruleName} · {detail.incident.target}</h3><Button variant="outline" onClick={() => navigate(basePath)}>{zh ? "返回事件列表" : "Back to incidents"}</Button></div><div className="alert-summary"><span>{zh ? "状态" : "State"}<Badge status={detail.incident.status} zh={zh} /></span><span>{zh ? "观测值" : "Observed"}<strong>{formatAlertObservedValue(detail.incident.metric, detail.incident.value, detail.incident.noData, zh)}</strong></span><span>{zh ? "开始时间" : "Started"}<strong>{stamp(detail.incident.startedAt, zh)}</strong></span></div>{!detail.incident.acknowledgedAt && !["resolved", "closed"].includes(detail.incident.status) && <Button variant="outline" disabled={busy} onClick={() => void act((signal) => postAlerting(`incidents/${encodeURIComponent(detail.incident.id)}/ack`, token, {}, signal), zh ? "事件已确认" : "Incident acknowledged")}>{zh ? "确认事件" : "Acknowledge"}</Button>}<ol>{detail.events.map((event) => <li key={event.id}><strong>{alertEventLabel(event.kind, zh)}</strong> · {stamp(event.at, zh)}<small>{typeof event.detail === "string" ? event.detail : JSON.stringify(event.detail)}</small></li>)}</ol>{!detail.events.length && <small>{zh ? "暂无事件" : "No events"}</small>}</div>}
      </section>}
      {tab === "rules" && <section className="panel">
        {!editor && <><div className="alert-toolbar"><h2>{zh ? "规则" : "Rules"}</h2><Button disabled={busy || !presets.length} onClick={() => navigate("/observe/rules/new")}>{zh ? "新建规则" : "Create rule"}</Button></div>
        <p className="alert-muted">{zh ? "阈值持续满足条件后触发，恢复时通知；重复通知间隔独立配置。未分配渠道的规则只记录告警。" : "Alerts fire after a condition holds for the configured duration, and notify on recovery. Rules without channels only record incidents."}</p>
        <div className="alert-table-scroll"><table className="alert-table"><thead><tr><th>{zh ? "规则 / 对象" : "Rule / target"}</th><th>{zh ? "条件" : "Condition"}</th><th>{zh ? "通知渠道" : "Channels"}</th><th>{zh ? "操作" : "Actions"}</th></tr></thead><tbody>{rules.map((item) => <tr key={item.id}><td className="alert-name">{item.name}<small>{item.target === "*" ? (zh ? "全部对象" : "All targets") : item.target}</small><small>{item.enabled ? (zh ? "已启用" : "Enabled") : (zh ? "已停用" : "Disabled")}{(item.silencedUntil || 0) > Date.now() / 1000 ? ` · ${zh ? "静默至" : "Silenced until"} ${stamp(item.silencedUntil, zh)}` : ""}</small></td><td>{presets.find((p) => p.metric === item.metric)?.name || item.metric}<small>&gt; {item.threshold} {presets.find((p) => p.metric === item.metric)?.unit || ""} · {item.forSeconds}s</small></td><td>{channelNames(item.channelIds) || (zh ? "仅记录" : "Record only")}</td><td><div className="alert-actions"><Button variant="outline" disabled={busy} onClick={() => navigate(`/observe/rules/${encodeURIComponent(item.id)}`)}>{zh ? "编辑" : "Edit"}</Button><Button variant="outline" disabled={busy} onClick={() => void act((signal) => postAlerting("rules", token, { ...item, enabled: !item.enabled }, signal))}>{item.enabled ? (zh ? "停用" : "Disable") : (zh ? "启用" : "Enable")}</Button><Button variant="outline" disabled={busy} onClick={() => void act((signal) => postAlerting("rules", token, { ...item, silencedUntil: (item.silencedUntil || 0) > Date.now() / 1000 ? 0 : Math.floor(Date.now() / 1000) + 3600 }, signal))}>{(item.silencedUntil || 0) > Date.now() / 1000 ? (zh ? "解除静默" : "Unsilence") : (zh ? "静默 1h" : "Silence 1h")}</Button></div></td></tr>)}</tbody></table></div>
        {!rules.length && <div className="alert-empty">{zh ? "尚未配置告警规则。可从磁盘、内存、节点失联等预设开始。" : "No rules yet. Start with a disk, memory or node heartbeat preset."}</div>}
        </>}
        {rule && <form className="alert-form" onSubmit={saveRule}><h3>{rule.id ? (zh ? "编辑规则" : "Edit rule") : (zh ? "新建规则" : "Create rule")}</h3><div className="alert-form-grid">
          <label>{zh ? "名称" : "Name"}<Input required value={rule.name} onChange={(e) => setRule({ ...rule, name: e.target.value })} /></label>
          <Field>
            <FieldLabel>{zh ? "指标预设" : "Metric preset"}</FieldLabel>
            <SelectControl
              className="min-w-0"
              value={rule.metric}
              onChange={(value) => { const preset = presets.find((p) => p.metric === value); setRule({ ...rule, metric: value, threshold: preset?.threshold ?? rule.threshold, forSeconds: preset?.forSeconds ?? rule.forSeconds }); }}
              options={presets.map((p) => ({ value: p.metric, label: p.name }))}
            />
            <small>{presets.find((p) => p.metric === rule.metric)?.description}</small>
          </Field>
          <label>{zh ? "对象 ID（* 表示全部）" : "Target ID (* for all)"}<Input list="alert-targets" required value={rule.target} onChange={(e) => setRule({ ...rule, target: e.target.value })} /><datalist id="alert-targets"><option value="*" />{(rule.metric.startsWith("node.") ? nodeNames : rule.metric === "task.queue_age" ? ["agent", "builder", "build"] : applicationNames).map((name) => <option value={name} key={name} />)}</datalist><small>{rule.metric.startsWith("node.") ? (zh ? "填写节点 ID；可从节点页面复制。" : "Use a node ID from the Fleet page.") : rule.metric === "task.queue_age" ? "agent / builder / build" : (zh ? "填写应用标识；* 匹配全部应用。" : "Application identifier; * matches all applications.")}</small></label>
          <label>{zh ? "阈值（观测值大于此值）" : "Threshold (observed value exceeds)"} · {presets.find((p) => p.metric === rule.metric)?.unit}<Input type="number" required min="0" step="any" value={Number.isNaN(rule.threshold) ? "" : rule.threshold} onChange={(e) => setRule({ ...rule, threshold: e.target.value === "" ? NaN : Number(e.target.value) })} /></label>
          <label>{zh ? "持续时间（秒）" : "Condition duration (seconds)"}<Input type="number" required min="0" step="1" value={Number.isNaN(rule.forSeconds) ? "" : rule.forSeconds} onChange={(e) => setRule({ ...rule, forSeconds: e.target.value === "" ? NaN : Number(e.target.value) })} /></label>
          <label>{zh ? "重复通知间隔（秒）" : "Repeat interval (seconds)"}<Input type="number" required min="60" max="604800" step="1" value={Number.isNaN(rule.repeatSeconds) ? "" : rule.repeatSeconds} onChange={(e) => setRule({ ...rule, repeatSeconds: e.target.value === "" ? NaN : Number(e.target.value) })} /></label>
          <Field>
            <FieldLabel>{zh ? "严重程度" : "Severity"}</FieldLabel>
            <SelectControl
              className="min-w-0"
              value={rule.severity}
              onChange={(value) => setRule({ ...rule, severity: value as AlertRule["severity"] })}
              options={[
                { value: "warning", label: zh ? "警告" : "Warning" },
                { value: "critical", label: zh ? "严重" : "Critical" },
              ]}
            />
          </Field>
          <Field>
            <FieldLabel>{zh ? "采样缺失时" : "When samples are missing"}</FieldLabel>
            <SelectControl
              className="min-w-0"
              value={rule.noData}
              onChange={(value) => setRule({ ...rule, noData: value as AlertRule["noData"] })}
              options={[
                { value: "keep", label: zh ? "保留当前状态" : "Keep current state" },
                { value: "alert", label: zh ? "触发缺失告警" : "Alert on missing data" },
              ]}
            />
          </Field>
        </div><fieldset><legend>{zh ? "通知渠道" : "Notification channels"}</legend>{channels.map((item) => <label className="alert-checkbox" key={item.id}><input type="checkbox" checked={rule.channelIds.includes(item.id)} onChange={(e) => setRule({ ...rule, channelIds: e.target.checked ? [...rule.channelIds, item.id] : rule.channelIds.filter((id) => id !== item.id) })} />{item.name}{!item.enabled ? (zh ? "（已停用）" : " (disabled)") : ""}</label>)}{!channels.length && <small>{zh ? "尚无渠道，请先到「通知渠道」配置飞书。" : "No channels. Configure Feishu under Notifications first."}</small>}</fieldset><label className="alert-checkbox"><input type="checkbox" checked={rule.enabled} onChange={(e) => setRule({ ...rule, enabled: e.target.checked })} />{zh ? "启用规则" : "Enable rule"}</label><div className="alert-actions"><Button type="submit" disabled={busy}>{busy ? (zh ? "保存中…" : "Saving…") : (zh ? "保存规则" : "Save rule")}</Button><Button variant="outline" type="button" disabled={busy} onClick={() => navigate(basePath)}>{zh ? "取消" : "Cancel"}</Button>{rule.id && <Button variant="outline" type="button" disabled={busy} onClick={() => { if (window.confirm(zh ? `删除规则「${rule.name}」？已有告警历史仍保留。` : `Delete rule “${rule.name}”? Existing incident history remains.`)) void act(async (signal) => { await deleteAlerting(`rules/${encodeURIComponent(rule.id)}`, token, signal); if (signal.aborted) return; setRule(null); setRefresh((n) => n + 1); navigate(basePath); }); }}>{zh ? "删除规则" : "Delete rule"}</Button>}</div></form>}
      </section>}
      {tab === "notifications" && <>
        <section className="panel">{!editor && <><div className="alert-toolbar"><h2>{zh ? "飞书通知渠道" : "Feishu notification channels"}</h2><Button disabled={busy} onClick={() => editChannel({ id: "", name: zh ? "飞书告警" : "Feishu alerts", type: "feishu", enabled: true, appId: "", chatId: "", appSecretConfigured: false })}>{zh ? "添加渠道" : "Add channel"}</Button></div><p className="alert-muted">{zh ? "填写应用的 App ID、App Secret 和群聊 ID。应用需启用并发布机器人，开通消息发送权限，并将机器人加入目标群；保存后可手动测试。App Secret 不会回显。" : "Enter the App ID, App Secret and chat ID. Enable and publish the app bot, grant message-sending permission and add it to the target group. Save, then send a test. The App Secret is never displayed."}</p>
        <div className="alert-table-scroll"><table className="alert-table"><thead><tr><th>{zh ? "渠道" : "Channel"}</th><th>{zh ? "配置" : "Configuration"}</th><th>{zh ? "操作" : "Actions"}</th></tr></thead><tbody>{channels.map((item) => <tr key={item.id}><td className="alert-name">{item.name}<small>{item.enabled ? (zh ? "已启用" : "Enabled") : (zh ? "已停用" : "Disabled")}</small></td><td>App ID: {item.appId}<small>{zh ? "群聊 ID" : "Chat ID"}: {item.chatId}</small><small>{item.appSecretConfigured ? (zh ? "App Secret 已配置" : "App Secret configured") : (zh ? "App Secret 缺失" : "App Secret missing")}</small></td><td><div className="alert-actions"><Button variant="outline" disabled={busy} onClick={() => editChannel(item)}>{zh ? "编辑" : "Edit"}</Button><Button variant="outline" disabled={busy || !item.enabled || !item.appSecretConfigured} onClick={() => void act(async (signal) => { const result = await postAlerting<{ delivery: AlertDelivery }>(`channels/${encodeURIComponent(item.id)}/test`, token, {}, signal); setNotice(`${zh ? "测试消息已排队，发送结果见下方记录。ID：" : "Test queued; check delivery status below. ID: "}${result.delivery.id}`); })}>{zh ? "发送测试消息" : "Send test message"}</Button></div></td></tr>)}</tbody></table></div>{!channels.length && <div className="alert-empty">{zh ? "尚未添加通知渠道。告警可先记录，配置渠道后才能推送到飞书。" : "No notification channels yet. Incidents can be recorded; Feishu delivery requires a channel."}</div>}
        </>}
        {channel && <form className="alert-form" onSubmit={saveChannel}><h3>{channel.id ? (zh ? "编辑渠道" : "Edit channel") : (zh ? "添加渠道" : "Add channel")}</h3><div className="alert-form-grid"><label>{zh ? "渠道名称（可选）" : "Channel name (optional)"}<Input value={channel.name} onChange={(e) => setChannel({ ...channel, name: e.target.value })} /></label><label>App ID<Input required autoComplete="off" value={channel.appId} placeholder="cli_…" onChange={(e) => setChannel({ ...channel, appId: e.target.value })} /></label><label>App Secret<Input type="password" autoComplete="new-password" required={!channel.appSecretConfigured} value={secret} placeholder={channel.appSecretConfigured ? (zh ? "已配置，留空保留" : "Configured; leave blank to keep") : ""} onChange={(e) => setSecret(e.target.value)} /></label><label>{zh ? "群聊 ID" : "Chat ID"}<Input required autoComplete="off" value={channel.chatId} placeholder="oc_…" onChange={(e) => setChannel({ ...channel, chatId: e.target.value })} /></label></div><label className="alert-checkbox"><input type="checkbox" checked={channel.enabled} onChange={(e) => setChannel({ ...channel, enabled: e.target.checked })} />{zh ? "启用渠道" : "Enable channel"}</label><div className="alert-actions"><Button type="submit" disabled={busy}>{zh ? "保存渠道" : "Save channel"}</Button><Button variant="outline" type="button" disabled={busy} onClick={() => navigate(basePath)}>{zh ? "取消" : "Cancel"}</Button>{channel.id && <Button variant="outline" type="button" disabled={busy} onClick={() => { if (window.confirm(zh ? `删除渠道「${channel.name}」？依赖此渠道的规则将无法通过它发送通知。` : `Delete channel “${channel.name}”? Rules will no longer be able to notify through it.`)) void act(async (signal) => { await deleteAlerting(`channels/${encodeURIComponent(channel.id)}`, token, signal); if (signal.aborted) return; setChannel(null); setSecret(""); setRefresh((n) => n + 1); navigate(basePath); }); }}>{zh ? "删除渠道" : "Delete channel"}</Button>}</div></form>}</section>
        {!editor && <section className="panel"><h2>{zh ? "通知发送记录" : "Delivery history"}</h2><small>{zh ? "排队不代表发送成功；失败原因与重试时间会在这里更新。发送失败时请检查机器人是否已入群、消息权限是否已发布。" : "Queued does not mean sent. Failures and retry times appear here. If delivery fails, check that the bot is in the group and message permissions are published."}</small><div className="alert-table-scroll"><table className="alert-table"><thead><tr><th>{zh ? "渠道 / 类型" : "Channel / kind"}</th><th>{zh ? "状态" : "State"}</th><th>{zh ? "时间 / 尝试" : "Time / attempts"}</th><th>{zh ? "详情" : "Details"}</th></tr></thead><tbody>{deliveries.items.map((item) => <tr key={item.id}><td>{channels.find((c) => c.id === item.channelId)?.name || item.channelId}<small>{alertEventLabel(item.kind, zh)}</small></td><td><Badge status={item.status} zh={zh} /></td><td>{stamp(item.sentAt || item.createdAt, zh)}<small>{item.attempts} {zh ? "次尝试" : "attempts"}</small></td><td className="alert-delivery-error">{item.lastError || "—"}{item.status !== "sent" && !!item.nextAttemptAt && <small>{zh ? "下次尝试" : "Next attempt"} · {stamp(item.nextAttemptAt, zh)}</small>}<small>{item.id}</small></td></tr>)}</tbody></table></div>{!deliveries.items.length && <div className="alert-empty">{zh ? "暂无发送记录。添加渠道后可发送一条测试消息。" : "No deliveries yet. Add a channel and send a test message."}</div>}{deliveries.nextCursor && <Button variant="outline" disabled={busy} onClick={() => void act(async (signal) => { const page = await getAlerting<AlertPage<AlertDelivery>>(`deliveries?limit=50&cursor=${encodeURIComponent(deliveries.nextCursor!)}`, token, signal); setDeliveries((old) => ({ ...page, items: mergeAlertPages(old.items, page.items) })); setExpanded(true); }, undefined, false)}>{zh ? "加载更多" : "Load more"}</Button>}</section>}
      </>}
    </>}
    {!editor && <div className="alert-refresh-footer"><span>{zh ? "每 15 秒自动刷新" : "Refreshes every 15 seconds"}</span><Button type="button" variant="ghost" size="sm" onClick={refreshNow} disabled={busy}>{zh ? "刷新" : "Refresh"}</Button></div>}
  </div>;
}
