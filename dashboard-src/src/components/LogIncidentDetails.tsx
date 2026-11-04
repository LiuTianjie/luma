import { toHref, useRouter } from "../router";
import { investigationPath } from "../observeContext";
import { alertLogExploreUrl, type LogAlertDetails } from "../alertingApi";
import { formatDateTime } from "../format";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

export function LogIncidentDetails({
  details,
  zh,
}: {
  details?: LogAlertDetails;
  zh: boolean;
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
            {zh ? "窗口源日志" : "Source lines in window"}
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
          ? "保留最近一次超过阈值的窗口，恢复后仍可追溯。最多展示 3 条代表性日志，长内容会截断；匹配条数不等于独立任务失败次数，重复日志可能合并为一个样例。时间为日志采集时间。"
          : "Evidence from the latest breached window is retained after recovery. Up to 3 representative logs are shown; long content is truncated. Matching lines are not unique task failures; duplicate logs may share one sample. Timestamps reflect collection time."}
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
          </div>
          {(() => {
            try {
              const data = JSON.parse(sample._msg || "");
              const id = data.trace_id || data.traceId;
              const span = data.span_id || data.spanId;
              const task =
                data.task_id || data.attributes?.["messaging.message.id"];
              const trace =
                typeof id === "string" && /^[a-f0-9]{32}$/i.test(id)
                  ? id.toLowerCase()
                  : "";
              if (!trace && !task) return null;
              return (
                <a
                  className="m-3 inline-block text-sm text-link hover:underline"
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
                      ? "查看关联 Trace"
                      : "Related trace"
                    : zh
                      ? "查看任务执行"
                      : "Task execution"}
                </a>
              );
            } catch {
              return null;
            }
          })()}
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all p-4 text-xs leading-relaxed">
            {sample._msg}
          </pre>
        </article>
      ))}
      {!samples.length && (
        <p className="text-sm text-muted-foreground">
          {zh
            ? "此窗口没有可展示的日志样例。"
            : "No samples were captured for this window."}
        </p>
      )}
      <Button
        variant="outline"
        className="self-start"
        nativeButton={false}
        render={
          <a
            href={alertLogExploreUrl(details)}
            target="_blank"
            rel="noreferrer"
          />
        }
      >
        {zh ? "检索此窗口日志" : "Search this log window"}
      </Button>
    </section>
  );
}
