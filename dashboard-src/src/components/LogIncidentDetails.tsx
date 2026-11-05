import { toHref, useRouter } from "../router";
import { investigationPath } from "../observeContext";
import { alertLogExploreUrl, type LogAlertDetails } from "../alertingApi";
import { formatDateTime } from "../format";
import { Badge } from "@/components/ui/badge";
import { useEffect, useState } from "react";
import { apiGet } from "../apiClient";
import type { LogAlertSample } from "../alertingApi";

type Evidence = { state: string; stack?: string; message?: string; location?: string; raw?: string; truncated?: boolean };
function FailureStack({ sample, details, token, zh }: { sample: LogAlertSample; details: LogAlertDetails; token: string; zh: boolean }) {
  const [result, setResult] = useState<Evidence>({ state: "loading" });
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!sample.trace_id || !sample.span_id) return;
    const controller = new AbortController();
    const at = sample._time ? Date.parse(sample._time) / 1000 : details.windowEnd;
    const observed = Number.isFinite(at) ? at : details.windowEnd;
    const query = new URLSearchParams({ app: details.app, task: sample.task || "", allocation: sample.alloc || "",
      since: String(Math.floor(Math.max(details.windowStart - 60, observed - 60))),
      until: String(Math.ceil(Math.min(Date.now() / 1000, details.windowEnd + 60, observed + 60))),
      contains: sample.span_id, limit: "100" });
    setResult({ state: "loading" });
    apiGet<{ entries: { line: string; truncated?: boolean }[]; page?: { hasMore?: boolean } }>(`/v1/logs/query?${query}`, token, controller.signal)
      .then(data => {
        let completion: Evidence | undefined;
        for (const row of data.entries) {
          try {
            const payload = JSON.parse(row.line);
            if (payload.trace_id !== sample.trace_id || payload.span_id !== sample.span_id ||
              (sample.task_id && payload.attributes?.["messaging.message.id"] !== sample.task_id)) continue;
            const evidence = { state: "ok", raw: JSON.stringify(payload, null, 2),
              message: payload.exception?.error_message || payload.attributes?.["error.message"] || payload.attributes?.["error.type"],
              location: payload.attributes?.["error.location"],
              stack: typeof payload.exception?.stacktrace === "string" ? payload.exception.stacktrace : undefined,
              truncated: row.truncated || payload.exception?.truncated };
            if (payload.event === "task.failure.detail") { setResult(evidence); return; }
            if (payload.event === "task.finished") completion = evidence;
          } catch { /* Only structured records from this exact execution are evidence. */ }
        }
        setResult(completion || { state: data.page?.hasMore ? "limited" : "missing" });
      }).catch(() => { if (!controller.signal.aborted) setResult({ state: "unavailable" }); });
    return () => controller.abort();
  }, [sample.trace_id, sample.span_id, sample.task_id, sample.task, sample.alloc, sample._time, details.app, details.windowStart, details.windowEnd, token, revision]);
  if (!sample.trace_id || !sample.span_id) return <div className="p-4 text-sm">
    {sample._msg.includes("\n…\n") ? <p className="text-muted-foreground">{zh ? "这条旧样例没有保留执行 ID，无法自动读取完整内容。请使用上方统计窗口旁的日志入口。" : "This older sample has no execution ID. Use the log link beside the evidence window to retrieve its full content."}</p>
      : <pre className="overflow-auto whitespace-pre-wrap break-all text-xs leading-relaxed">{sample._msg}</pre>}
  </div>;
  return <div className="flex flex-col gap-3 p-4 text-sm">
    {result.message && <p className="wrap-anywhere font-medium">{result.message}</p>}
    {result.location && <code className="wrap-anywhere text-xs text-muted-foreground">{result.location}</code>}
    {result.truncated && <p className="text-xs text-muted-foreground">{zh ? "应用上报的原始异常已截断。" : "The exception was truncated at its source."}</p>}
    {result.stack ? <pre className="overflow-x-auto whitespace-pre-wrap break-all text-xs leading-relaxed">{result.stack}</pre>
      : <p className="text-xs text-muted-foreground">{result.state === "loading" ? (zh ? "正在读取此执行的原始异常" : "Reading this attempt's exception")
        : result.state === "unavailable" ? (zh ? "异常日志查询暂不可用。" : "Exception query is temporarily unavailable.")
          : result.state === "limited" ? (zh ? "查询达到条数上限，可查看 Trace 或使用统计窗口旁的日志入口。" : "Query limit reached. Open the trace or the window's logs.")
            : (zh ? "保留日志中没有找到本次执行的堆栈，可查看 Trace 中的异常事件。" : "No stack was found in retained logs for this attempt. Check its trace exception events.")}</p>}
    {result.state === "unavailable" && <button type="button" className="self-start text-xs text-link hover:underline" onClick={() => setRevision(n => n + 1)}>{zh ? "重试读取" : "Retry"}</button>}
    {result.raw && <details className="text-xs">
      <summary className="cursor-pointer text-muted-foreground">{zh ? "原始日志" : "Original log"}</summary>
      <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all leading-relaxed">{result.raw}</pre>
    </details>}
  </div>;
}

export function LogIncidentDetails({
  details,
  zh,
  token,
}: {
  details?: LogAlertDetails;
  zh: boolean;
  token: string;
}) {
  const { search } = useRouter();
  if (!details?.sampledAt)
    return (
      <p className="text-sm text-muted-foreground">
        {zh
          ? "此历史告警未保存日志样例。仍在触发的告警会在下一次有效采样时补充；不会用当前日志冒充历史记录。"
          : "No log evidence was saved for this historical incident. Active incidents are enriched by the next valid sample."}
      </p>
    );
  const samples = details.samples || [];
  const stamp = (value: number) => formatDateTime(value, zh ? "zh" : "en");
  return (
    <section
      className="flex min-w-0 flex-col gap-4"
      aria-label={zh ? "异常日志详情" : "Log evidence"}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 className="text-base font-semibold">
          {zh ? "异常日志" : "Log evidence"}
        </h3>
        <Badge variant="secondary">
          {zh
            ? `窗口匹配 ${details.matchCount} 条 · 展示 ${samples.length} 条`
            : `${details.matchCount} matches · ${samples.length} samples`}
        </Badge>
      </div>
      <dl className="grid gap-4 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-muted-foreground">
            {zh ? "应用 / 服务" : "Application / task"}
          </dt>
          <dd className="mt-1 wrap-anywhere">
            {details.app} / {details.task || (zh ? "全部服务" : "All tasks")}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">
            {zh ? "异常统计窗口" : "Evidence window"}
          </dt>
          <dd className="mt-1">
            {stamp(details.windowStart)} — {stamp(details.windowEnd)}
            <a className="ml-3 text-xs text-link hover:underline" href={alertLogExploreUrl(details)} target="_blank" rel="noreferrer">{zh ? "检索此窗口日志" : "Window logs"}</a>
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">
            {zh ? "采样时间" : "Sampled at"}
          </dt>
          <dd className="mt-1">{stamp(details.sampledAt)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">
            {zh ? "窗口内执行 / 日志" : "Attempts / lines in window"}
          </dt>
          <dd className="mt-1">{details.total ?? "—"}</dd>
        </div>
      </dl>
      {details.query && (
        <p className="text-sm wrap-anywhere text-muted-foreground">
          {Object.entries(details.query)
            .map(
              ([key, value]) =>
                `${({ contains: zh ? "包含" : "Contains", exclude: zh ? "排除" : "Excludes", field: zh ? "字段" : "Field", equals: zh ? "等于" : "Equals" } as Record<string, string>)[key] || key}: ${value}`,
            )
            .join(" · ")}
        </p>
      )}
      <p className="text-sm text-muted-foreground">
        {zh
          ? "保留最近一次超过阈值的窗口，恢复后仍可追溯。每条样例按执行身份读取原始异常；Trace 和堆栈的保留时间可能不同。时间为日志采集时间。"
          : "The latest breached window is retained after recovery. Each sample reads the original exception for its execution. Trace and log retention may differ. Timestamps reflect collection time."}
      </p>
      {details.historicalBackfill && (
        <p className="text-sm text-muted-foreground">
          {zh
            ? "此记录根据原告警时间窗口补采。"
            : "Evidence was backfilled from the original incident window."}
        </p>
      )}
      {samples.map((sample, index) => (
        <article
          key={index}
          className="min-w-0 overflow-hidden rounded-lg border"
        >
          <div className="flex flex-wrap gap-x-3 gap-y-1 border-b bg-muted/40 px-4 py-2 text-xs text-muted-foreground">
            <span className="font-medium text-foreground">
              {zh ? `样例 ${index + 1}` : `Sample ${index + 1}`}
            </span>
            <span>
              {sample._time
                ? formatDateTime(sample._time, zh ? "zh" : "en")
                : "—"}
            </span>
            <span className="wrap-anywhere">
              {sample.task} · {sample.stream}
            </span>
            {sample.alloc && (
              <span className="wrap-anywhere">alloc {sample.alloc}</span>
            )}
          {(() => {
            try {
              const data = sample.trace_id ? {} : JSON.parse(sample._msg || "");
              const id = sample.trace_id || data.trace_id || data.traceId;
              const span = sample.span_id || data.span_id || data.spanId;
              const task =
                sample.task_id || data.task_id || data.attributes?.["messaging.message.id"];
              const trace =
                typeof id === "string" && /^[a-f0-9]{32}$/i.test(id)
                  ? id.toLowerCase()
                  : "";
              if (!trace && !task) return null;
              return (
                <div className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 pt-1">
                  {trace && <code className="wrap-anywhere text-xs" translate="no">Trace {trace}</code>}
                <a
                  className="inline-flex shrink-0 items-center text-xs text-link hover:underline"
                  href={toHref(
                    investigationPath(
                      `/apps/${encodeURIComponent(details.app)}/dashboards`,
                      search,
                      {
                        observeView: trace ? "traces" : "events",
                        observeTrace: trace,
                        observeSpan:
                          trace && /^[a-f0-9]{16}$/i.test(span || "")
                            ? span.toLowerCase()
                            : "",
                        observe_task_id: typeof task === "string" ? task : "",
                        observe_since: String(details.windowStart),
                        observe_until: String(details.windowEnd),
                      },
                    ),
                  )}
                >
                  {trace
                    ? zh
                      ? "查看 Trace"
                      : "View trace"
                    : zh
                      ? "查看任务执行"
                      : "Task execution"}
                </a>
                </div>
              );
            } catch {
              return null;
            }
          })()}
          </div>
          <FailureStack sample={sample} details={details} token={token} zh={zh} />
        </article>
      ))}
      {!samples.length && (
        <p className="text-sm text-muted-foreground">
          {zh
            ? "此窗口没有可展示的日志样例。"
            : "No samples were captured for this window."}
        </p>
      )}

    </section>
  );
}
