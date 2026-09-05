import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";
import { ArrowDown, RefreshCw, RotateCcw, Search, Square } from "lucide-react";
import { cancelBuildRun, retryBuildRunStream } from "../deploy/deployApi";
import type { DeployStep } from "../deploy/types";
import { StepLog } from "../deploy/StepLog";
import { Badge, CodeCell, StatePill } from "../components/ui";
import { PageHeader } from "./PageHeader";
import { formatTimestamp } from "../format";
import { t } from "../i18n";
import { useRouter } from "../router";
import { OverlayShell } from "../useOverlay";
import { fetchHistory, fetchHistoryDetail } from "../historyApi";
import { dateInputTimestamp, HISTORY_FILTERS, historyFilters, historyItemKey, historySelection, historySelectionSearch, historyRetentionNotice, historyStatus, historyStatusValue, localDateInput, mergeHistoryItems, retryBuildSelection, type HistoryDetail, type HistoryItem, type HistoryPage, type HistorySelection } from "../historyModel";
import type { Lang } from "../types";
import "../history.css";

type LoadMode = "refresh" | "more";
const EMPTY_PAGE: HistoryPage = { limit: 50, nextCursor: null, hasMore: false };
const messageOf = (error: unknown) => error instanceof Error ? error.message : String(error);

function sourceLabel(source: string | undefined, lang: Lang) {
  if (source === "build") return lang === "zh" ? "构建" : "Build";
  if (source === "dashboard") return lang === "zh" ? "控制台" : "Dashboard";
  return source === "cli" ? "CLI" : source || "-";
}

function HistoryFilters({ lang, filters, onApply }: { lang: Lang; filters: string; onApply: (filters: URLSearchParams) => void }) {
  const zh = lang === "zh";
  const params = new URLSearchParams(filters);
  const [validation, setValidation] = useState("");
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const next = new URLSearchParams();
    for (const name of HISTORY_FILTERS) {
      const raw = String(values.get(name) || "").trim();
      const value = name === "since" || name === "until" ? dateInputTimestamp(raw) : raw;
      if (value) next.set(name, value);
    }
    if (next.has("since") && next.has("until") && new Date(next.get("since")!).getTime() > new Date(next.get("until")!).getTime()) {
      setValidation(zh ? "结束时间不能早于开始时间。" : "End time must be after start time.");
      return;
    }
    setValidation("");
    onApply(next);
  };
  return <form className="history-filter-form" onSubmit={submit}>
    <div className="history-filter-fields">
      <label className="history-app-filter"><span>{zh ? "应用" : "Application"}</span><input name="app" defaultValue={params.get("app") || ""} placeholder={zh ? "应用名称" : "Application name"} /></label>
      <label><span>{zh ? "记录类型" : "Record type"}</span><select name="kind" defaultValue={params.get("kind") || ""}>
        <option value="">{zh ? "全部类型" : "All types"}</option><option value="build">{zh ? "构建" : "Build"}</option><option value="deployment">{zh ? "部署" : "Deployment"}</option>
      </select></label>
      <label><span>{zh ? "来源" : "Source"}</span><select name="source" defaultValue={params.get("source") || ""}>
        <option value="">{zh ? "全部来源" : "All sources"}</option><option value="build">{sourceLabel("build", lang)}</option><option value="cli">CLI</option><option value="dashboard">{sourceLabel("dashboard", lang)}</option>
      </select></label>
      <label><span>{t(lang, "status")}</span><select name="status" defaultValue={params.get("status") || ""}>
        <option value="">{zh ? "全部状态" : "All statuses"}</option>
        {Array.from(new Set(["queued", "running", "active", "succeeded", "failed", "failed_partial", "canceling", "canceled", params.get("status") || ""])).filter(Boolean).map((status) => <option key={status} value={status}>{historyStatus(status, lang)}</option>)}
      </select></label>
      <label><span>{zh ? "开始时间" : "From"}</span><input type="datetime-local" name="since" defaultValue={localDateInput(params.get("since") || "")} /></label>
      <label><span>{zh ? "结束时间" : "Until"}</span><input type="datetime-local" name="until" defaultValue={localDateInput(params.get("until") || "")} /></label>
    </div>
    <div className="history-filter-actions">
      <small>{zh ? "时间按本地时区显示；筛选条件保存在链接中。" : "Times use your local timezone. Filters are saved in the URL."}</small>
      <button type="button" className="ghost" onClick={() => onApply(new URLSearchParams())} disabled={!filters}>{zh ? "清空筛选" : "Clear filters"}</button>
      <button type="submit" className="primary"><Search size={15} aria-hidden="true" />{zh ? "筛选记录" : "Apply filters"}</button>
    </div>
    {validation ? <p className="history-validation" role="alert">{validation}</p> : null}
  </form>;
}

function HistoryDetailDrawer({ lang, token, selection, initialItem, onClose, onRefresh, onSelect }: {
  lang: Lang; token: string; selection: HistorySelection; initialItem?: HistoryItem; onClose: () => void; onRefresh: () => void; onSelect: (entry: HistorySelection) => void;
}) {
  const zh = lang === "zh";
  const [data, setData] = useState<HistoryDetail | null>(null);
  const [loading, setLoading] = useState<LoadMode | null>("refresh");
  const [error, setError] = useState("");
  const [failedMode, setFailedMode] = useState<LoadMode>("refresh");
  const [action, setAction] = useState<"retry" | "cancel" | null>(null);
  const [actionError, setActionError] = useState("");
  const [actionNotice, setActionNotice] = useState("");
  const [retrySteps, setRetrySteps] = useState<DeployStep[]>([]);
  const [retryTarget, setRetryTarget] = useState<HistorySelection | null>(null);
  const mounted = useRef(true);
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const current = useRef(data);
  current.current = data;
  const request = useRef<{ controller: AbortController; id: number } | null>(null);
  const sequence = useRef(0);
  const appliedPages = useRef(new Set<string>());
  const kind = selection.kind;
  const id = selection.id;
  const load = useCallback(async (mode: LoadMode) => {
    if (mode === "more" && (request.current || !current.current?.page.nextCursor)) return;
    request.current?.controller.abort();
    const controller = new AbortController();
    const requestId = ++sequence.current;
    request.current = { controller, id: requestId };
    const cursor = mode === "more" ? current.current?.page.nextCursor : null;
    setLoading(mode);
    setError("");
    try {
      const result = await fetchHistoryDetail(token, { kind, id }, cursor, controller.signal);
      if (requestId !== sequence.current) return;
      const pageKey = cursor || "";
      const alreadyApplied = appliedPages.current.has(pageKey);
      if (mode === "refresh") appliedPages.current.clear();
      appliedPages.current.add(pageKey);
      setData((previous) => mode === "more" && previous
        ? { ...result, events: alreadyApplied ? previous.events : [...previous.events, ...result.events] }
        : result);
    } catch (cause) {
      if (controller.signal.aborted || requestId !== sequence.current) return;
      setError(messageOf(cause));
      setFailedMode(mode);
    } finally {
      if (requestId === sequence.current) { request.current = null; setLoading(null); }
    }
  }, [token, kind, id]);
  useEffect(() => {
    void load("refresh");
    return () => { sequence.current += 1; request.current?.controller.abort(); request.current = null; };
  }, [load]);

  const item = data?.item || initialItem;
  const retentionNotice = historyRetentionNotice(item, lang);
  const retryable = item?.kind === "build" && ["failed", "failed_partial", "canceled", "cancelled", "interrupted", "error"].includes(item.status || "");
  const cancelable = item?.kind === "build" && ["running", "canceling"].includes(item.status || "");
  const retryBuild = async () => {
    setAction("retry"); setActionError(""); setActionNotice(""); setRetrySteps([]); setRetryTarget(null);
    try {
      const result = await retryBuildRunStream(token, id, (step) => {
        if (!mounted.current) return;
        setRetrySteps((steps) => [...steps, step]);
        const target = retryBuildSelection(step, id) || retryBuildSelection(step.result, id);
        if (target) setRetryTarget(target);
      });
      onRefresh();
      if (!mounted.current) return;
      const target = retryBuildSelection(result, id);
      if (target) selectRef.current(target);
      else setActionNotice(zh ? "重试完成，最新记录已刷新。" : "Retry completed. The latest records were refreshed.");
    } catch (cause) {
      onRefresh();
      if (mounted.current) setActionError(messageOf(cause));
    }
    finally { if (mounted.current) setAction(null); }
  };
  const cancelBuild = async () => {
    setAction("cancel"); setActionError(""); setActionNotice("");
    try {
      await cancelBuildRun(token, id);
      setActionNotice(zh ? "已提交取消请求。" : "Cancellation requested.");
      await load("refresh");
      onRefresh();
    } catch (cause) { setActionError(messageOf(cause)); }
    finally { setAction(null); }
  };
  return createPortal(<OverlayShell className="detail-backdrop" onClose={onClose}>
    {(ref) => <aside className="detail-drawer history-detail-drawer" ref={ref} role="dialog" aria-modal="true" aria-labelledby="history-detail-title" onClick={(event) => event.stopPropagation()}>
      <header><div><p className="eyebrow">{kind === "build" ? (zh ? "构建记录" : "Build record") : (zh ? "部署记录" : "Deployment record")}</p><h2 id="history-detail-title">{item?.title || item?.application || id}</h2></div><button type="button" className="icon-button" onClick={onClose}>{t(lang, "close")}</button></header>
      {item ? <dl>
        <div><dt>{zh ? "应用" : "Application"}</dt><dd>{item.application || "-"}</dd></div>
        <div><dt>{zh ? "来源" : "Source"}</dt><dd>{sourceLabel(item.source, lang)}</dd></div>
        <div><dt>{t(lang, "status")}</dt><dd><StatePill label={historyStatus(item.status, lang)} value={historyStatusValue(item.status)} /></dd></div>
        <div><dt>{zh ? "创建时间" : "Created"}</dt><dd>{formatTimestamp(item.createdAt, lang)}</dd></div>
        {item.updatedAt ? <div><dt>{zh ? "最近更新" : "Updated"}</dt><dd>{formatTimestamp(item.updatedAt, lang)}</dd></div> : null}
        {item.repository ? <div><dt>{zh ? "仓库" : "Repository"}</dt><dd>{item.repository}</dd></div> : null}
        {item.ref ? <div><dt>{zh ? "版本引用" : "Ref"}</dt><dd>{item.ref}</dd></div> : null}
        {item.buildNode ? <div><dt>{zh ? "构建节点" : "Build node"}</dt><dd>{item.buildNode}</dd></div> : null}
        <div><dt>ID</dt><dd><CodeCell value={id} /></dd></div>
        {item.retryOf && item.retryOf !== id ? <div><dt>{zh ? "重试来源" : "Retry of"}</dt><dd><button type="button" className="ghost text-link-button" disabled={Boolean(action)} onClick={() => onSelect({ kind: "build", id: item.retryOf! })}>{zh ? "查看上一次尝试" : "View previous attempt"}</button></dd></div> : null}
      </dl> : null}
      {item?.message ? <p className="history-record-message">{item.message}</p> : null}
      <div className="history-detail-actions">
        <button type="button" className="ghost" disabled={Boolean(loading || action)} onClick={() => void load("refresh")}><RefreshCw size={15} className={loading === "refresh" ? "spin" : undefined} aria-hidden="true" />{zh ? "刷新详情" : "Refresh detail"}</button>
        {retryable ? <button type="button" className="ghost" disabled={Boolean(action)} onClick={() => void retryBuild()}><RotateCcw size={15} aria-hidden="true" />{action === "retry" ? (zh ? "重试中…" : "Retrying…") : (zh ? "按原参数重试" : "Retry build")}</button> : null}
        {cancelable ? <button type="button" className="ghost danger" disabled={Boolean(action) || item?.status === "canceling"} onClick={() => void cancelBuild()}><Square size={14} aria-hidden="true" />{action === "cancel" || item?.status === "canceling" ? (zh ? "正在取消…" : "Canceling…") : (zh ? "取消构建" : "Cancel build")}</button> : null}
      </div>
      {actionError ? <div className="alert alert-error" role="alert">{actionError}</div> : null}
      {actionNotice ? <div className="alert alert-success" role="status">{actionNotice}</div> : null}
      {retryTarget && action !== "retry" ? <button type="button" className="ghost" onClick={() => onSelect(retryTarget)}>{zh ? "查看本次重试记录" : "View this retry attempt"}</button> : null}
      {retrySteps.length ? <section><h3>{zh ? "本次重试日志" : "Current retry log"}</h3><StepLog steps={retrySteps} lang={lang} /></section> : null}
      <section className="history-events-section" aria-busy={Boolean(loading)}>
        <div className="history-section-heading"><h3>{zh ? "步骤日志" : "Step log"}</h3><small>{zh ? `已加载 ${data?.events.length || 0}${typeof item?.stepCount === "number" ? ` / ${item.stepCount}` : ""} 条` : `${data?.events.length || 0}${typeof item?.stepCount === "number" ? ` of ${item.stepCount}` : ""} events loaded`}</small></div>
        <small className="history-event-order">{zh ? "从最早事件开始显示；刷新详情会重新加载第一页。" : "Events start with the earliest. Refreshing detail reloads the first page."}</small>
        {error ? <div className="alert alert-error" role="alert"><span>{data ? (zh ? "更新失败，已保留现有日志。" : "Update failed. Previously loaded events are retained.") : (zh ? "详情加载失败。" : "Could not load this record.")} {error}</span><button type="button" className="ghost" disabled={Boolean(loading)} onClick={() => void load(failedMode)}>{zh ? "重试加载" : "Retry loading"}</button></div> : null}
        {retentionNotice ? <div className="history-retention-note" role="status"><p>{retentionNotice}</p><small>{zh ? "清理时间" : "Removed at"} · {formatTimestamp(item?.detailsExpiredAt, lang)}</small></div> : null}
        {data?.events.length ? <StepLog steps={data.events} lang={lang} /> : loading ? <p role="status">{zh ? "正在加载步骤日志…" : "Loading step log…"}</p> : data && !error && !retentionNotice ? <p className="deployment-config-empty">{zh ? "这条记录没有分步日志。" : "No step log was recorded."}</p> : null}
        {data?.page.hasMore ? <button type="button" className="ghost history-load-more" disabled={Boolean(loading)} onClick={() => void load("more")}><ArrowDown size={15} aria-hidden="true" />{loading === "more" ? (zh ? "加载中…" : "Loading…") : (zh ? "加载后续步骤" : "Load more events")}</button> : null}
      </section>
    </aside>}
  </OverlayShell>, document.body);
}

export function DeploymentsPage({ lang, token }: { lang: Lang; token: string }) {
  const zh = lang === "zh";
  const { path, search, navigate } = useRouter();
  const filters = historyFilters(search).toString();
  const selection = historySelection(search);
  const [list, setList] = useState<{ filters: string; items: HistoryItem[]; page: HistoryPage; loadedAt: number }>({ filters, items: [], page: EMPTY_PAGE, loadedAt: 0 });
  const [loading, setLoading] = useState<LoadMode | null>("refresh");
  const [error, setError] = useState("");
  const [failedMode, setFailedMode] = useState<LoadMode>("refresh");
  const current = useRef(list);
  current.current = list;
  const sequence = useRef(0);
  const request = useRef<AbortController | null>(null);
  const load = useCallback(async (mode: LoadMode) => {
    if (mode === "more" && (request.current || current.current.filters !== filters || !current.current.page.nextCursor)) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const requestId = ++sequence.current;
    const cursor = mode === "more" ? current.current.page.nextCursor : null;
    setLoading(mode); setError("");
    try {
      const result = await fetchHistory(token, filters, cursor, controller.signal);
      if (sequence.current !== requestId) return;
      setList((previous) => ({ filters, items: mergeHistoryItems(mode === "more" && previous.filters === filters ? previous.items : [], result.items), page: result.page, loadedAt: Date.now() / 1000 }));
    } catch (cause) {
      if (controller.signal.aborted || sequence.current !== requestId) return;
      setError(messageOf(cause)); setFailedMode(mode);
    } finally {
      if (sequence.current === requestId) { request.current = null; setLoading(null); }
    }
  }, [token, filters]);
  useEffect(() => {
    void load("refresh");
    return () => { sequence.current += 1; request.current?.abort(); request.current = null; };
  }, [load]);
  const navigateSearch = (query: string) => navigate(`${path}${query ? `?${query}` : ""}`);
  const select = (entry: HistorySelection | null) => navigateSearch(historySelectionSearch(search, entry));
  const staleFilters = list.filters !== filters && Boolean(list.loadedAt);
  const apply = (next: URLSearchParams) => {
    const params = new URLSearchParams(search);
    for (const name of HISTORY_FILTERS) { params.delete(name); if (next.has(name)) params.set(name, next.get(name)!); }
    navigateSearch(params.toString());
  };
  return <>
    <PageHeader meta={{ eyebrow: zh ? "部署记录" : "Deployments", title: zh ? "部署与构建时间线" : "Deployment and build timeline", description: zh ? "按应用、来源、状态和时间查询；记录与步骤日志按需分页。" : "Search by application, source, status, and time. Records and event logs load in pages.", metrics: [], action: <button type="button" className="ghost" onClick={() => void load("refresh")} disabled={Boolean(loading)}><RefreshCw size={16} aria-hidden="true" className={loading === "refresh" ? "spin" : undefined} />{zh ? "刷新最新记录" : "Refresh latest"}</button> }} />
    <article className="panel deployments-panel history-panel">
      <HistoryFilters key={filters} lang={lang} filters={filters} onApply={apply} />
      {error ? <div className="alert alert-error" role="alert"><span>{list.loadedAt ? (zh ? "加载失败，已保留上次读取的记录。" : "Loading failed. Previously loaded records are retained.") : (zh ? "历史记录加载失败。" : "History could not be loaded.")} {error}</span><button type="button" className="ghost" disabled={Boolean(loading)} onClick={() => void load(failedMode)}>{zh ? "重试加载" : "Retry loading"}</button></div> : null}
      {staleFilters ? <p className="history-stale" role="status">{zh ? "新筛选结果尚未加载，下方仍为上次筛选的记录。" : "The new filter results have not loaded. The previous results remain below."}</p> : null}
      {!list.items.length && loading ? <div className="page-loading-inline" aria-busy="true"><span className="skeleton skeleton-line" /><span className="skeleton skeleton-line skeleton-medium" /><p>{zh ? "正在加载记录…" : "Loading records…"}</p></div> : null}
      {list.items.length ? <ol className="deployments-timeline" aria-busy={Boolean(loading)}>
        {list.items.map((item) => <li className="deployments-row is-clickable" key={historyItemKey(item)} role="button" tabIndex={0} aria-label={`${t(lang, "details")}: ${item.title || item.application || item.id}`} onClick={() => select(item)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(item); } }}>
          <span className="deployments-origin"><Badge value={sourceLabel(item.source, lang)} /></span>
          <div className="deployments-main"><CodeCell value={item.title || item.application || item.id} /><small>{[item.kind === "build" ? (zh ? "构建" : "Build") : (zh ? "部署" : "Deployment"), item.application, item.ref, item.buildNode].filter(Boolean).join(" · ")}</small></div>
          <StatePill label={historyStatus(item.status, lang)} value={historyStatusValue(item.status)} />
          <time className="deployments-time" title={zh ? "创建时间" : "Created time"}>{formatTimestamp(item.createdAt, lang)}</time>
        </li>)}
      </ol> : !loading && !error ? <div className="empty-inline">{filters ? (zh ? "没有符合筛选条件的记录。" : "No records match these filters.") : (zh ? "暂无部署或构建记录。" : "No deployment or build records yet.")}</div> : null}
      {list.loadedAt ? <footer className="history-pagination"><span>{zh ? `已加载 ${list.items.length} 条 · ${formatTimestamp(list.loadedAt, lang)}` : `${list.items.length} records loaded · ${formatTimestamp(list.loadedAt, lang)}`}</span>{list.page.hasMore && !staleFilters ? <button type="button" className="ghost" disabled={Boolean(loading)} onClick={() => void load("more")}><ArrowDown size={15} aria-hidden="true" />{loading === "more" ? (zh ? "加载中…" : "Loading…") : (zh ? "加载更早记录" : "Load older records")}</button> : !staleFilters ? <small>{zh ? "已到当前查询末尾" : "End of current results"}</small> : null}</footer> : null}
    </article>
    {selection ? <HistoryDetailDrawer key={historyItemKey(selection)} lang={lang} token={token} selection={selection} initialItem={list.items.find((item) => historyItemKey(item) === historyItemKey(selection))} onClose={() => select(null)} onRefresh={() => void load("refresh")} onSelect={select} /> : null}
  </>;
}
