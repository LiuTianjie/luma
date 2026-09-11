import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowDown, GitBranch, Plus, RefreshCw, RotateCcw, Search, Square } from "lucide-react";
import { cancelBuildRun, retryBuildRunStream } from "../deploy/deployApi";
import type { DeployStep } from "../deploy/types";
import { StepLog } from "../deploy/StepLog";
import { DateTimePicker } from "../components/DateTimePicker";
import { Badge, CodeCell, SelectControl, StatePill } from "../components/primitives";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { PageHeader } from "./PageHeader";
import { formatTimestamp } from "../format";
import { t } from "../i18n";
import { useRouter } from "../router";
import { fetchHistory, fetchHistoryDetail } from "../historyApi";
import { dateInputTimestamp, HISTORY_FILTERS, historyFilters, historyItemKey, historySelection, historyRetentionNotice, historyStatus, historyStatusValue, localDateInput, mergeHistoryItems, retryBuildSelection, type HistoryDetail, type HistoryItem, type HistoryPage, type HistorySelection } from "../historyModel";
import type { Lang } from "../types";

type LoadMode = "refresh" | "more";
const EMPTY_PAGE: HistoryPage = { limit: 50, nextCursor: null, hasMore: false };
const messageOf = (error: unknown) => error instanceof Error ? error.message : String(error);
const STATUS_OPTIONS = ["queued", "running", "active", "succeeded", "failed", "failed_partial", "canceling", "canceled"];

function sourceLabel(source: string | undefined, lang: Lang) {
  if (source === "build") return lang === "zh" ? "构建" : "Build";
  if (source === "dashboard") return lang === "zh" ? "控制台" : "Dashboard";
  return source === "cli" ? "CLI" : source || "-";
}

function HistoryFilters({ lang, filters, onApply }: { lang: Lang; filters: string; onApply: (filters: URLSearchParams) => void }) {
  const zh = lang === "zh";
  const params = new URLSearchParams(filters);
  const [app, setApp] = useState(params.get("app") || "");
  const [kind, setKind] = useState(params.get("kind") || "");
  const [source, setSource] = useState(params.get("source") || "");
  const [status, setStatus] = useState(params.get("status") || "");
  const [since, setSince] = useState(localDateInput(params.get("since") || ""));
  const [until, setUntil] = useState(localDateInput(params.get("until") || ""));
  const [validation, setValidation] = useState("");
  const statuses = Array.from(new Set([...STATUS_OPTIONS, status].filter(Boolean)));
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const values = { app, kind, source, status, since, until };
    const next = new URLSearchParams();
    for (const name of HISTORY_FILTERS) {
      const raw = String(values[name] || "").trim();
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
  const clear = () => {
    setApp("");
    setKind("");
    setSource("");
    setStatus("");
    setSince("");
    setUntil("");
    setValidation("");
    onApply(new URLSearchParams());
  };
  return (
    <form className="flex flex-col gap-4" onSubmit={submit}>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-6">
        <Field>
          <FieldLabel>{zh ? "应用" : "Application"}</FieldLabel>
          <Input value={app} onChange={(event) => setApp(event.target.value)} placeholder={zh ? "应用名称" : "Application name"} />
        </Field>
        <Field>
          <FieldLabel>{zh ? "记录类型" : "Record type"}</FieldLabel>
          <SelectControl
            value={kind}
            onChange={setKind}
            className="min-w-0"
            options={[
              { value: "", label: zh ? "全部类型" : "All types" },
              { value: "build", label: zh ? "构建" : "Build" },
              { value: "deployment", label: zh ? "部署" : "Deployment" },
            ]}
          />
        </Field>
        <Field>
          <FieldLabel>{zh ? "来源" : "Source"}</FieldLabel>
          <SelectControl
            value={source}
            onChange={setSource}
            className="min-w-0"
            options={[
              { value: "", label: zh ? "全部来源" : "All sources" },
              { value: "build", label: sourceLabel("build", lang) },
              { value: "cli", label: "CLI" },
              { value: "dashboard", label: sourceLabel("dashboard", lang) },
            ]}
          />
        </Field>
        <Field>
          <FieldLabel>{t(lang, "status")}</FieldLabel>
          <SelectControl
            value={status}
            onChange={setStatus}
            className="min-w-0"
            options={[
              { value: "", label: zh ? "全部状态" : "All statuses" },
              ...statuses.map((item) => ({ value: item, label: historyStatus(item, lang) })),
            ]}
          />
        </Field>
        <Field>
          <FieldLabel>{zh ? "开始时间" : "From"}</FieldLabel>
          <DateTimePicker lang={lang} value={since} onChange={setSince} defaultTime="00:00" placeholder={zh ? "选择开始时间" : "Pick start time"} />
        </Field>
        <Field>
          <FieldLabel>{zh ? "结束时间" : "Until"}</FieldLabel>
          <DateTimePicker lang={lang} value={until} onChange={setUntil} defaultTime="23:59" placeholder={zh ? "选择结束时间" : "Pick end time"} />
        </Field>
      </div>
      <div className="flex flex-wrap items-center justify-end gap-2">
        <FieldDescription className="mr-auto">
          {zh ? "时间按本地时区显示；筛选条件保存在链接中。" : "Times use your local timezone. Filters are saved in the URL."}
        </FieldDescription>
        <Button type="button" variant="outline" onClick={clear} disabled={!filters}>
          {zh ? "清空筛选" : "Clear filters"}
        </Button>
        <Button type="submit">
          <Search />
          {zh ? "筛选记录" : "Apply filters"}
        </Button>
      </div>
      {validation ? <FieldError>{validation}</FieldError> : null}
    </form>
  );
}

function HistoryDetailPage({ lang, token, selection, initialItem, onClose, onRefresh, onSelect }: {
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

  const refreshAfterAction = useRef(false);
  useEffect(() => {
    if (!action && refreshAfterAction.current) {
      refreshAfterAction.current = false;
      void load("refresh");
    }
    const refresh = () => {
      if (action) refreshAfterAction.current = true;
      else void load("refresh");
    };
    window.addEventListener("luma:refresh", refresh);
    return () => window.removeEventListener("luma:refresh", refresh);
  }, [action, load]);

  const item = data?.item || initialItem;
  const retentionNotice = historyRetentionNotice(item, lang);
  const retryable = item?.kind === "build" && ["failed", "failed_partial", "canceled", "cancelled", "interrupted", "error"].includes(item.status || "");
  const cancelable = item?.kind === "build" && ["queued", "running", "canceling"].includes(item.status || "");
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
  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader meta={{
        eyebrow: zh ? "交付 / 任务详情" : "Delivery / Task",
        title: item?.title || item?.application || id,
        description: kind === "build" ? (zh ? "构建记录与完整步骤日志" : "Build record and step log") : (zh ? "部署记录与完整步骤日志" : "Deployment record and step log"),
        metrics: [],
        action: <Button type="button" variant="outline" onClick={onClose}>{zh ? "返回交付记录" : "Back to delivery"}</Button>,
      }} />
      {item ? (
        <dl className="grid grid-cols-1 gap-x-6 md:grid-cols-2">
          <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-3 border-b py-3 text-sm"><dt className="text-muted-foreground">{zh ? "应用" : "Application"}</dt><dd className="min-w-0 break-all">{item.application || "-"}</dd></div>
          <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-3 border-b py-3 text-sm"><dt className="text-muted-foreground">{zh ? "来源" : "Source"}</dt><dd>{sourceLabel(item.source, lang)}</dd></div>
          <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-3 border-b py-3 text-sm"><dt className="text-muted-foreground">{t(lang, "status")}</dt><dd><StatePill label={historyStatus(item.status, lang)} value={historyStatusValue(item.status)} /></dd></div>
          <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-3 border-b py-3 text-sm"><dt className="text-muted-foreground">{zh ? "创建时间" : "Created"}</dt><dd>{formatTimestamp(item.createdAt, lang)}</dd></div>
          {item.updatedAt ? <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-3 border-b py-3 text-sm"><dt className="text-muted-foreground">{zh ? "最近更新" : "Updated"}</dt><dd>{formatTimestamp(item.updatedAt, lang)}</dd></div> : null}
          {item.repository ? <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-3 border-b py-3 text-sm"><dt className="text-muted-foreground">{zh ? "仓库" : "Repository"}</dt><dd className="min-w-0 break-all">{item.repository}</dd></div> : null}
          {item.ref ? <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-3 border-b py-3 text-sm"><dt className="text-muted-foreground">{zh ? "版本引用" : "Ref"}</dt><dd>{item.ref}</dd></div> : null}
          {item.buildNode ? <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-3 border-b py-3 text-sm"><dt className="text-muted-foreground">{zh ? "构建节点" : "Build node"}</dt><dd>{item.buildNode}</dd></div> : null}
          <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-3 border-b py-3 text-sm"><dt className="text-muted-foreground">ID</dt><dd><CodeCell value={id} /></dd></div>
          {item.retryOf && item.retryOf !== id ? (
            <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-3 border-b py-3 text-sm">
              <dt className="text-muted-foreground">{zh ? "重试来源" : "Retry of"}</dt>
              <dd>
                <Button type="button" variant="link" className="h-auto px-0" disabled={Boolean(action)} onClick={() => onSelect({ kind: "build", id: item.retryOf! })}>
                  {zh ? "查看上一次尝试" : "View previous attempt"}
                </Button>
              </dd>
            </div>
          ) : null}
        </dl>
      ) : null}
      {item?.message ? <p className="rounded-lg bg-muted px-3 py-2 text-sm">{item.message}</p> : null}
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" disabled={Boolean(loading || action)} onClick={() => void load("refresh")}>
          <RefreshCw className={loading === "refresh" ? "animate-spin" : undefined} />
          {zh ? "刷新详情" : "Refresh detail"}
        </Button>
        {retryable ? (
          <Button type="button" variant="outline" disabled={Boolean(action)} onClick={() => void retryBuild()}>
            <RotateCcw />
            {action === "retry" ? (zh ? "重试中…" : "Retrying…") : (zh ? "按原参数重试" : "Retry build")}
          </Button>
        ) : null}
        {cancelable ? (
          <Button type="button" variant="destructive" disabled={Boolean(action) || item?.status === "canceling"} onClick={() => void cancelBuild()}>
            <Square />
            {action === "cancel" || item?.status === "canceling" ? (zh ? "正在取消…" : "Canceling…") : (zh ? "取消构建" : "Cancel build")}
          </Button>
        ) : null}
      </div>
      {actionError ? <Alert variant="destructive"><AlertDescription>{actionError}</AlertDescription></Alert> : null}
      {actionNotice ? <Alert><AlertDescription>{actionNotice}</AlertDescription></Alert> : null}
      {retryTarget && action !== "retry" ? (
        <Button type="button" variant="outline" onClick={() => onSelect(retryTarget)}>{zh ? "查看本次重试记录" : "View this retry attempt"}</Button>
      ) : null}
      {retrySteps.length ? <section className="flex flex-col gap-3"><h3 className="text-sm font-medium">{zh ? "本次重试日志" : "Current retry log"}</h3><StepLog steps={retrySteps} lang={lang} /></section> : null}
      <section className="flex flex-col gap-3" aria-busy={Boolean(loading)}>
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="text-sm font-medium">{zh ? "步骤日志" : "Step log"}</h3>
          <p className="text-xs text-muted-foreground">{zh ? `已加载 ${data?.events.length || 0}${typeof item?.stepCount === "number" ? ` / ${item.stepCount}` : ""} 条` : `${data?.events.length || 0}${typeof item?.stepCount === "number" ? ` of ${item.stepCount}` : ""} events loaded`}</p>
        </div>
        <p className="text-xs text-muted-foreground">{zh ? "从最早事件开始显示；刷新详情会重新加载第一页。" : "Events start with the earliest. Refreshing detail reloads the first page."}</p>
        {error ? (
          <Alert variant="destructive">
            <AlertDescription className="flex flex-wrap items-center gap-2">
              <span>{data ? (zh ? "更新失败，已保留现有日志。" : "Update failed. Previously loaded events are retained.") : (zh ? "详情加载失败。" : "Could not load this record.")} {error}</span>
              <Button type="button" variant="outline" size="sm" disabled={Boolean(loading)} onClick={() => void load(failedMode)}>{zh ? "重试加载" : "Retry loading"}</Button>
            </AlertDescription>
          </Alert>
        ) : null}
        {retentionNotice ? (
          <Alert>
            <AlertDescription>
              <p>{retentionNotice}</p>
              <p className="mt-1 text-xs">{zh ? "清理时间" : "Removed at"} · {formatTimestamp(item?.detailsExpiredAt, lang)}</p>
            </AlertDescription>
          </Alert>
        ) : null}
        {data?.events.length ? <StepLog steps={data.events} lang={lang} /> : loading ? <p role="status" className="text-sm text-muted-foreground">{zh ? "正在加载步骤日志…" : "Loading step log…"}</p> : data && !error && !retentionNotice ? <p className="text-sm text-muted-foreground">{zh ? "这条记录没有分步日志。" : "No step log was recorded."}</p> : null}
        {data?.page.hasMore ? (
          <Button type="button" variant="outline" disabled={Boolean(loading)} onClick={() => void load("more")}>
            <ArrowDown />
            {loading === "more" ? (zh ? "加载中…" : "Loading…") : (zh ? "加载后续步骤" : "Load more events")}
          </Button>
        ) : null}
      </section>
    </div>
  );
}

export function DeploymentsPage({ lang, token }: { lang: Lang; token: string }) {
  const zh = lang === "zh";
  const { path, search, navigate } = useRouter();
  const filters = historyFilters(search).toString();
  const segments = path.split("/").filter(Boolean);
  let pathSelection: HistorySelection | null = null;
  if (segments.length === 3 && (segments[1] === "build" || segments[1] === "deployment")) {
    try { pathSelection = { kind: segments[1], id: decodeURIComponent(segments[2]) }; } catch { /* Invalid URL remains a list. */ }
  }
  const selection = pathSelection || historySelection(search);
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
  const showingDetail = Boolean(selection);
  useEffect(() => {
    if (showingDetail) return;
    const refresh = () => { void load("refresh"); };
    window.addEventListener("luma:refresh", refresh);
    return () => window.removeEventListener("luma:refresh", refresh);
  }, [showingDetail, load]);
  const navigateSearch = (query: string) => navigate(`${path}${query ? `?${query}` : ""}`);
  const select = (entry: HistorySelection | null) => navigate(`/deployments${entry ? `/${entry.kind}/${encodeURIComponent(entry.id)}` : ""}${filters ? `?${filters}` : ""}`);
  const staleFilters = list.filters !== filters && Boolean(list.loadedAt);
  const apply = (next: URLSearchParams) => {
    const params = new URLSearchParams(search);
    for (const name of HISTORY_FILTERS) { params.delete(name); if (next.has(name)) params.set(name, next.get(name)!); }
    navigateSearch(params.toString());
  };
  if (selection) return <HistoryDetailPage key={historyItemKey(selection)} lang={lang} token={token} selection={selection} initialItem={list.items.find((item) => historyItemKey(item) === historyItemKey(selection))} onClose={() => select(null)} onRefresh={() => void load("refresh")} onSelect={select} />;
  return (
    <>
      <PageHeader meta={{
        eyebrow: zh ? "部署记录" : "Deployments",
        title: zh ? "部署与构建时间线" : "Deployment and build timeline",
        description: zh ? "按应用、来源、状态和时间查询；记录与步骤日志按需分页。" : "Search by application, source, status, and time. Records and event logs load in pages.",
        metrics: [],
        action: (
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" variant="outline" onClick={() => void load("refresh")} disabled={Boolean(loading)}>
              <RefreshCw className={loading === "refresh" ? "animate-spin" : undefined} />
              {zh ? "刷新最新记录" : "Refresh latest"}
            </Button>
            <Button type="button" variant="outline" onClick={() => navigate("/builds")}>
              <GitBranch />
              {zh ? "从 Git 构建" : "Build from Git"}
            </Button>
            <Button type="button" onClick={() => navigate("/create")}>
              <Plus />
              {zh ? "创建应用" : "Create application"}
            </Button>
          </div>
        ),
      }} />
      <Card>
        <CardContent className="flex flex-col gap-4">
          <HistoryFilters key={filters} lang={lang} filters={filters} onApply={apply} />
          {error ? (
            <Alert variant="destructive">
              <AlertDescription className="flex flex-wrap items-center gap-2">
                <span>{list.loadedAt ? (zh ? "加载失败，已保留上次读取的记录。" : "Loading failed. Previously loaded records are retained.") : (zh ? "历史记录加载失败。" : "History could not be loaded.")} {error}</span>
                <Button type="button" variant="outline" size="sm" disabled={Boolean(loading)} onClick={() => void load(failedMode)}>{zh ? "重试加载" : "Retry loading"}</Button>
              </AlertDescription>
            </Alert>
          ) : null}
          {staleFilters ? <p className="text-sm text-muted-foreground" role="status">{zh ? "新筛选结果尚未加载，下方仍为上次筛选的记录。" : "The new filter results have not loaded. The previous results remain below."}</p> : null}
          {!list.items.length && loading ? <p className="text-sm text-muted-foreground" role="status">{zh ? "正在加载记录…" : "Loading records…"}</p> : null}
          {list.items.length ? (
            <Table aria-busy={Boolean(loading)}>
              <TableHeader>
                <TableRow>
                  <TableHead>{zh ? "来源" : "Source"}</TableHead>
                  <TableHead>{zh ? "记录" : "Record"}</TableHead>
                  <TableHead>{zh ? "状态" : "Status"}</TableHead>
                  <TableHead className="text-right">{zh ? "时间" : "Time"}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {list.items.map((item) => (
                  <TableRow
                    key={historyItemKey(item)}
                    className="cursor-pointer"
                    tabIndex={0}
                    aria-label={`${t(lang, "details")}: ${item.title || item.application || item.id}`}
                    onClick={() => select(item)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        select(item);
                      }
                    }}
                  >
                    <TableCell><Badge value={sourceLabel(item.source, lang)} /></TableCell>
                    <TableCell>
                      <div className="min-w-0">
                        <CodeCell value={item.title || item.application || item.id} />
                        <p className="mt-0.5 truncate text-xs text-muted-foreground">{[item.kind === "build" ? (zh ? "构建" : "Build") : (zh ? "部署" : "Deployment"), item.application, item.ref, item.buildNode].filter(Boolean).join(" · ")}</p>
                      </div>
                    </TableCell>
                    <TableCell><StatePill label={historyStatus(item.status, lang)} value={historyStatusValue(item.status)} /></TableCell>
                    <TableCell className="text-right text-xs text-muted-foreground">
                      <time title={zh ? "创建时间" : "Created time"}>{formatTimestamp(item.createdAt, lang)}</time>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : !loading && !error ? (
            <Empty className="py-8">
              <EmptyHeader>
                <EmptyTitle>{filters ? (zh ? "没有符合筛选条件的记录。" : "No records match these filters.") : (zh ? "暂无部署或构建记录。" : "No deployment or build records yet.")}</EmptyTitle>
                <EmptyDescription>{zh ? "调整筛选条件，或从 Git 构建 / 创建应用开始。" : "Adjust filters, or start from a Git build or a new application."}</EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : null}
        </CardContent>
        {list.loadedAt ? (
          <CardFooter className="flex flex-wrap items-center justify-between gap-2 border-t">
            <span className="text-xs text-muted-foreground">{zh ? `已加载 ${list.items.length} 条 · ${formatTimestamp(list.loadedAt, lang)}` : `${list.items.length} records loaded · ${formatTimestamp(list.loadedAt, lang)}`}</span>
            {list.page.hasMore && !staleFilters ? (
              <Button type="button" variant="outline" size="sm" disabled={Boolean(loading)} onClick={() => void load("more")}>
                <ArrowDown />
                {loading === "more" ? (zh ? "加载中…" : "Loading…") : (zh ? "加载更早记录" : "Load older records")}
              </Button>
            ) : !staleFilters ? <small className="text-xs text-muted-foreground">{zh ? "已到当前查询末尾" : "End of current results"}</small> : null}
          </CardFooter>
        ) : null}
      </Card>
    </>
  );
}
