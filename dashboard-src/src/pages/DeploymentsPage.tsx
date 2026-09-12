import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ArrowDown, ArrowLeft, GitBranch, Plus, RefreshCw, RotateCcw, ScrollText, Search, Square } from "lucide-react";
import { cancelBuildRun, retryBuildRunStream } from "../deploy/deployApi";
import type { DeployStep } from "../deploy/types";
import { StepLog } from "../deploy/StepLog";
import { DateTimePicker } from "../components/DateTimePicker";
import { CodeCell, StatePill } from "../components/primitives";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldTitle } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { PageHeader } from "./PageHeader";
import { formatTimestamp } from "../format";
import { t } from "../i18n";
import { toHref, useRouter } from "../router";
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
  const fieldId = useId();
  const params = new URLSearchParams(filters);
  const [app, setApp] = useState(params.get("app") || "");
  const [kind, setKind] = useState(params.get("kind") || "");
  const [source, setSource] = useState(params.get("source") || "");
  const [status, setStatus] = useState(params.get("status") || "");
  const [since, setSince] = useState(localDateInput(params.get("since") || ""));
  const [until, setUntil] = useState(localDateInput(params.get("until") || ""));
  const [validation, setValidation] = useState("");
  const statuses = Array.from(new Set([...STATUS_OPTIONS, status].filter(Boolean)));
  const statusItems = [{ value: "all", label: zh ? "全部状态" : "All statuses" }, ...statuses.map((item) => ({ value: item, label: historyStatus(item, lang) }))];
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
    <form onSubmit={submit}>
      <FieldGroup>
      <FieldGroup className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <Field>
          <FieldLabel htmlFor={`${fieldId}-app`}>{zh ? "应用" : "Application"}</FieldLabel>
          <Input id={`${fieldId}-app`} value={app} onChange={(event) => setApp(event.target.value)} placeholder={zh ? "应用名称" : "Application name"} />
        </Field>
        <Field>
          <FieldTitle id={`${fieldId}-kind`}>{zh ? "记录类型" : "Record type"}</FieldTitle>
          <ToggleGroup aria-labelledby={`${fieldId}-kind`} variant="outline" value={[kind || "all"]} onValueChange={(values) => setKind(values[0] === "all" ? "" : values[0] || "")} className="flex-wrap">
            <ToggleGroupItem value="all">{zh ? "全部" : "All"}</ToggleGroupItem>
            <ToggleGroupItem value="build">{zh ? "构建" : "Build"}</ToggleGroupItem>
            <ToggleGroupItem value="deployment">{zh ? "部署" : "Deployment"}</ToggleGroupItem>
            {kind && !["build", "deployment"].includes(kind) ? <ToggleGroupItem value={kind}>{kind}</ToggleGroupItem> : null}
          </ToggleGroup>
        </Field>
        <Field>
          <FieldTitle id={`${fieldId}-source`}>{zh ? "来源" : "Source"}</FieldTitle>
          <ToggleGroup aria-labelledby={`${fieldId}-source`} variant="outline" value={[source || "all"]} onValueChange={(values) => setSource(values[0] === "all" ? "" : values[0] || "")} className="flex-wrap">
            <ToggleGroupItem value="all">{zh ? "全部" : "All"}</ToggleGroupItem>
            <ToggleGroupItem value="build">{sourceLabel("build", lang)}</ToggleGroupItem>
            <ToggleGroupItem value="cli">CLI</ToggleGroupItem>
            <ToggleGroupItem value="dashboard">{sourceLabel("dashboard", lang)}</ToggleGroupItem>
            {source && !["build", "cli", "dashboard"].includes(source) ? <ToggleGroupItem value={source}>{source}</ToggleGroupItem> : null}
          </ToggleGroup>
        </Field>
        <Field>
          <FieldLabel htmlFor={`${fieldId}-status`}>{t(lang, "status")}</FieldLabel>
          <Select items={statusItems} value={status || "all"} onValueChange={(value) => setStatus(value === "all" ? "" : value || "")}>
            <SelectTrigger id={`${fieldId}-status`} className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent><SelectGroup>{statusItems.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}</SelectGroup></SelectContent>
          </Select>
        </Field>
        <Field>
          <FieldLabel htmlFor={`${fieldId}-since`}>{zh ? "开始时间" : "From"}</FieldLabel>
          <DateTimePicker id={`${fieldId}-since`} lang={lang} value={since} onChange={setSince} defaultTime="00:00" placeholder={zh ? "选择开始时间" : "Pick start time"} />
        </Field>
        <Field data-invalid={Boolean(validation)}>
          <FieldLabel htmlFor={`${fieldId}-until`}>{zh ? "结束时间" : "Until"}</FieldLabel>
          <DateTimePicker id={`${fieldId}-until`} aria-invalid={Boolean(validation)} aria-describedby={validation ? `${fieldId}-error` : undefined} lang={lang} value={until} onChange={setUntil} defaultTime="23:59" placeholder={zh ? "选择结束时间" : "Pick end time"} />
          {validation ? <FieldError id={`${fieldId}-error`}>{validation}</FieldError> : null}
        </Field>
      </FieldGroup>
      <FieldDescription>
          {zh ? "时间按本地时区显示；筛选条件保存在链接中。" : "Times use your local timezone. Filters are saved in the URL."}
      </FieldDescription>
      <Field orientation="horizontal" className="flex-wrap justify-end">
        <Button type="button" variant="outline" onClick={clear} disabled={!app && !kind && !source && !status && !since && !until}>
          {zh ? "清空筛选" : "Clear filters"}
        </Button>
        <Button type="submit">
          <Search data-icon="inline-start" />
          {zh ? "筛选记录" : "Apply filters"}
        </Button>
      </Field>
      </FieldGroup>
    </form>
  );
}

function HistoryFact({ label, children }: { label: string; children: ReactNode }) {
  return <div className="flex min-w-0 flex-col gap-1"><dt className="text-sm text-muted-foreground">{label}</dt><dd className="m-0 min-w-0 text-sm wrap-break-word">{children}</dd></div>;
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
        action: <Button type="button" variant="outline" onClick={onClose}><ArrowLeft data-icon="inline-start" />{zh ? "返回交付记录" : "Back to delivery"}</Button>,
      }} />
      <Card aria-busy={!item && Boolean(loading)}>
        <CardHeader><CardTitle>{zh ? "任务信息" : "Task information"}</CardTitle></CardHeader>
        <CardContent className="flex flex-col gap-4">
          {item ? (
            <dl className="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
              <HistoryFact label={zh ? "应用" : "Application"}>{item.application || "-"}</HistoryFact>
              <HistoryFact label={zh ? "来源" : "Source"}>{sourceLabel(item.source, lang)}</HistoryFact>
              <HistoryFact label={t(lang, "status")}><StatePill label={historyStatus(item.status, lang)} value={historyStatusValue(item.status)} /></HistoryFact>
              <HistoryFact label={zh ? "创建时间" : "Created"}>{formatTimestamp(item.createdAt, lang)}</HistoryFact>
              {item.updatedAt ? <HistoryFact label={zh ? "最近更新" : "Updated"}>{formatTimestamp(item.updatedAt, lang)}</HistoryFact> : null}
              {item.repository ? <HistoryFact label={zh ? "仓库" : "Repository"}>{item.repository}</HistoryFact> : null}
              {item.ref ? <HistoryFact label={zh ? "版本引用" : "Ref"}>{item.ref}</HistoryFact> : null}
              {item.buildNode ? <HistoryFact label={zh ? "构建节点" : "Build node"}>{item.buildNode}</HistoryFact> : null}
              <HistoryFact label="ID"><CodeCell value={id} /></HistoryFact>
              {item.retryOf && item.retryOf !== id ? <HistoryFact label={zh ? "重试来源" : "Retry of"}>
                <Button type="button" variant="link" className="h-auto px-0" disabled={Boolean(action)} onClick={() => onSelect({ kind: "build", id: item.retryOf! })}>
                  {zh ? "查看上一次尝试" : "View previous attempt"}
                </Button>
              </HistoryFact> : null}
            </dl>
          ) : loading ? (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3" role="status">
              <span className="sr-only">{zh ? "正在加载任务信息…" : "Loading task information…"}</span>
              {Array.from({ length: 6 }, (_, index) => <Skeleton key={index} className="h-12 w-full" />)}
            </div>
          ) : <Empty><EmptyHeader><EmptyTitle>{zh ? "未能读取任务信息" : "Task information unavailable"}</EmptyTitle><EmptyDescription>{zh ? "请重试加载这条记录。" : "Try loading this record again."}</EmptyDescription></EmptyHeader></Empty>}
          {item?.message ? <Alert><AlertDescription>{item.message}</AlertDescription></Alert> : null}
        </CardContent>
        <CardFooter className="flex-wrap gap-2">
          <Button type="button" variant="outline" disabled={Boolean(loading || action)} onClick={() => void load("refresh")}>
            {loading === "refresh" ? <Spinner data-icon="inline-start" aria-hidden="true" /> : <RefreshCw data-icon="inline-start" />}
            {zh ? "刷新详情" : "Refresh detail"}
          </Button>
          {retryable ? (
            <Button type="button" variant="outline" disabled={Boolean(action)} onClick={() => void retryBuild()}>
              {action === "retry" ? <Spinner data-icon="inline-start" aria-hidden="true" /> : <RotateCcw data-icon="inline-start" />}
              {action === "retry" ? (zh ? "重试中…" : "Retrying…") : (zh ? "按原参数重试" : "Retry build")}
            </Button>
          ) : null}
          {cancelable ? (
            <Button type="button" variant="destructive" disabled={Boolean(action) || item?.status === "canceling"} onClick={() => void cancelBuild()}>
              {action === "cancel" || item?.status === "canceling" ? <Spinner data-icon="inline-start" aria-hidden="true" /> : <Square data-icon="inline-start" />}
              {action === "cancel" || item?.status === "canceling" ? (zh ? "正在取消…" : "Canceling…") : (zh ? "取消构建" : "Cancel build")}
            </Button>
          ) : null}
        </CardFooter>
      </Card>
      {actionError ? <Alert variant="destructive"><AlertDescription>{actionError}</AlertDescription></Alert> : null}
      {actionNotice ? <Alert><AlertDescription>{actionNotice}</AlertDescription></Alert> : null}
      {retryTarget && action !== "retry" ? (
        <Button type="button" variant="outline" className="w-fit" onClick={() => onSelect(retryTarget)}>{zh ? "查看本次重试记录" : "View this retry attempt"}</Button>
      ) : null}
      {retrySteps.length ? <Card><CardHeader><CardTitle>{zh ? "本次重试日志" : "Current retry log"}</CardTitle></CardHeader><CardContent><StepLog steps={retrySteps} lang={lang} /></CardContent></Card> : null}
      <Card aria-busy={Boolean(loading)}>
        <CardHeader>
          <CardTitle>{zh ? "步骤日志" : "Step log"}</CardTitle>
          <CardDescription>{zh ? "从最早事件开始显示；刷新详情会重新加载第一页。" : "Events start with the earliest. Refreshing detail reloads the first page."}</CardDescription>
          <CardAction><Badge variant="secondary">{zh ? `已加载 ${data?.events.length || 0}${typeof item?.stepCount === "number" ? ` / ${item.stepCount}` : ""} 条` : `${data?.events.length || 0}${typeof item?.stepCount === "number" ? ` of ${item.stepCount}` : ""} events loaded`}</Badge></CardAction>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
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
            <AlertDescription className="flex flex-col gap-2">
              <span>{retentionNotice}</span>
              <span>{zh ? "清理时间" : "Removed at"} · {formatTimestamp(item?.detailsExpiredAt, lang)}</span>
            </AlertDescription>
          </Alert>
        ) : null}
        {data?.events.length ? <StepLog steps={data.events} lang={lang} /> : loading ? (
          <div className="flex flex-col gap-3" role="status"><span className="sr-only">{zh ? "正在加载步骤日志…" : "Loading step log…"}</span>{Array.from({ length: 3 }, (_, index) => <Skeleton key={index} className="h-10 w-full" />)}</div>
        ) : data && !error && !retentionNotice ? <Empty><EmptyHeader><EmptyMedia variant="icon"><ScrollText /></EmptyMedia><EmptyTitle>{zh ? "这条记录没有分步日志" : "No step log was recorded"}</EmptyTitle><EmptyDescription>{zh ? "任务的状态和时间仍可在上方查看。" : "The task status and timestamps remain available above."}</EmptyDescription></EmptyHeader></Empty> : null}
        </CardContent>
        {data?.page.hasMore ? (
          <CardFooter>
          <Button type="button" variant="outline" disabled={Boolean(loading)} onClick={() => void load("more")}>
            {loading === "more" ? <Spinner data-icon="inline-start" aria-hidden="true" /> : <ArrowDown data-icon="inline-start" />}
            {loading === "more" ? (zh ? "加载中…" : "Loading…") : (zh ? "加载后续步骤" : "Load more events")}
          </Button>
          </CardFooter>
        ) : null}
      </Card>
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
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader meta={{
        eyebrow: zh ? "部署记录" : "Deployments",
        title: zh ? "部署与构建时间线" : "Deployment and build timeline",
        description: zh ? "按应用、来源、状态和时间查询；记录与步骤日志按需分页。" : "Search by application, source, status, and time. Records and event logs load in pages.",
        metrics: [],
        action: (
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" variant="outline" onClick={() => void load("refresh")} disabled={Boolean(loading)}>
              {loading === "refresh" ? <Spinner data-icon="inline-start" aria-hidden="true" /> : <RefreshCw data-icon="inline-start" />}
              {zh ? "刷新最新记录" : "Refresh latest"}
            </Button>
            <Button type="button" variant="outline" onClick={() => navigate("/builds")}>
              <GitBranch data-icon="inline-start" />
              {zh ? "从 Git 构建" : "Build from Git"}
            </Button>
            <Button type="button" onClick={() => navigate("/create")}>
              <Plus data-icon="inline-start" />
              {zh ? "创建应用" : "Create application"}
            </Button>
          </div>
        ),
      }} />
      <Card>
        <CardHeader>
          <CardTitle>{zh ? "筛选记录" : "Filter records"}</CardTitle>
          <CardDescription>{zh ? "选择需要查看的应用、记录类型和时间范围。" : "Choose the application, record type, and time range to review."}</CardDescription>
        </CardHeader>
        <CardContent><HistoryFilters key={filters} lang={lang} filters={filters} onApply={apply} /></CardContent>
      </Card>
      <Card aria-busy={Boolean(loading)}>
        <CardHeader>
          <CardTitle>{zh ? "交付记录" : "Delivery records"}</CardTitle>
          <CardDescription>{zh ? "按创建时间从新到旧排列。" : "Ordered by creation time, newest first."}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {error ? (
            <Alert variant="destructive">
              <AlertDescription className="flex flex-wrap items-center gap-2">
                <span>{list.loadedAt ? (zh ? "加载失败，已保留上次读取的记录。" : "Loading failed. Previously loaded records are retained.") : (zh ? "历史记录加载失败。" : "History could not be loaded.")} {error}</span>
                <Button type="button" variant="outline" size="sm" disabled={Boolean(loading)} onClick={() => void load(failedMode)}>{zh ? "重试加载" : "Retry loading"}</Button>
              </AlertDescription>
            </Alert>
          ) : null}
          {staleFilters ? <Alert role="status"><AlertDescription>{zh ? "新筛选结果尚未加载，下方仍为上次筛选的记录。" : "The new filter results have not loaded. The previous results remain below."}</AlertDescription></Alert> : null}
          {!list.items.length && loading ? <div className="flex flex-col gap-3" role="status"><span className="sr-only">{zh ? "正在加载记录…" : "Loading records…"}</span>{Array.from({ length: 5 }, (_, index) => <Skeleton key={index} className="h-12 w-full" />)}</div> : null}
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
                  <TableRow key={historyItemKey(item)}>
                    <TableCell><Badge variant="secondary">{sourceLabel(item.source, lang)}</Badge></TableCell>
                    <TableCell>
                      <div className="flex min-w-0 max-w-96 flex-col items-start gap-1">
                        <Button variant="link" size="sm" className="h-auto max-w-full justify-start px-0" nativeButton={false} render={<a href={toHref(`/deployments/${item.kind}/${encodeURIComponent(item.id)}${filters ? `?${filters}` : ""}`)} onClick={(event) => { if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); select(item); } }} />}>
                          <span className="truncate" title={item.title || item.application || item.id}>{item.title || item.application || item.id}</span>
                        </Button>
                        <p className="max-w-full truncate text-xs text-muted-foreground" title={[item.application, item.ref, item.buildNode].filter(Boolean).join(" · ")}>{[item.kind === "build" ? (zh ? "构建" : "Build") : (zh ? "部署" : "Deployment"), item.application, item.ref, item.buildNode].filter(Boolean).join(" · ")}</p>
                      </div>
                    </TableCell>
                    <TableCell><StatePill label={historyStatus(item.status, lang)} value={historyStatusValue(item.status)} /></TableCell>
                    <TableCell className="text-right">
                      <time title={zh ? "创建时间" : "Created time"}>{formatTimestamp(item.createdAt, lang)}</time>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : !loading && !error ? (
            <Empty>
              <EmptyHeader>
                <EmptyMedia variant="icon"><ScrollText /></EmptyMedia>
                <EmptyTitle>{filters ? (zh ? "没有符合筛选条件的记录。" : "No records match these filters.") : (zh ? "暂无部署或构建记录。" : "No deployment or build records yet.")}</EmptyTitle>
                <EmptyDescription>{zh ? "调整筛选条件，或从 Git 构建 / 创建应用开始。" : "Adjust filters, or start from a Git build or a new application."}</EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : null}
        </CardContent>
        {list.loadedAt ? (
          <CardFooter className="flex-wrap justify-between gap-2">
            <span className="text-xs text-muted-foreground">{zh ? `已加载 ${list.items.length} 条 · ${formatTimestamp(list.loadedAt, lang)}` : `${list.items.length} records loaded · ${formatTimestamp(list.loadedAt, lang)}`}</span>
            {list.page.hasMore && !staleFilters ? (
              <Button type="button" variant="outline" size="sm" disabled={Boolean(loading)} onClick={() => void load("more")}>
                {loading === "more" ? <Spinner data-icon="inline-start" aria-hidden="true" /> : <ArrowDown data-icon="inline-start" />}
                {loading === "more" ? (zh ? "加载中…" : "Loading…") : (zh ? "加载更早记录" : "Load older records")}
              </Button>
            ) : !staleFilters ? <small className="text-xs text-muted-foreground">{zh ? "已到当前查询末尾" : "End of current results"}</small> : null}
          </CardFooter>
        ) : null}
      </Card>
    </div>
  );
}
