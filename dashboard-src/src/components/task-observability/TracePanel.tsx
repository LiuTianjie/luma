import {
  ArrowUpRight,
  ChevronDown,
  ChevronRightIcon,
} from "@/components/icons";
import { useMemo, useState } from "react";
import { useRouter } from "../../router";
import { grafanaRange, investigationPath } from "../../observeContext";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SelectControl } from "../primitives";
import {
  AttributeTable,
  Outcome,
  ReadState,
  timeText,
  useObservation,
} from "./shared";
import { buildTrace, milliseconds, spanDuration } from "./traceModel";
import type { BoardProps, LogsResult, TraceDetail } from "./types";

export function TraceLogs({
  app,
  token,
  zh,
  traceId,
  hours,
  until,
}: BoardProps & { traceId: string; hours: number; until?: number }) {
  const [revision, setRevision] = useState(0);
  const [filter, setFilter] = useState("");
  const { data, loading, error } = useObservation<LogsResult>(
    "logs",
    app,
    token,
    { trace_id: traceId, hours, ...(until ? { until } : {}) },
    revision,
  );
  const rows =
    data?.items?.filter(
      (row) => !filter || row.line.toLowerCase().includes(filter.toLowerCase()),
    ) || [];
  return (
    <section className="observe-log-panel">
      <div className="observe-toolbar">
        <Input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder={zh ? "在已读取日志中查找" : "Find in loaded logs"}
          aria-label={zh ? "筛选日志" : "Filter logs"}
        />
        <Button
          variant="outline"
          size="sm"
          disabled={loading}
          onClick={() => setRevision((n) => n + 1)}
        >
          {zh ? "重新读取" : "Reload"}
        </Button>
      </div>
      <ReadState
        loading={loading}
        state={error || data?.state}
        zh={zh}
        retry={() => setRevision((n) => n + 1)}
      />
      {!loading && data?.state === "ok" && (
        <>
          {data.diagnostic && (
            <div className="observe-failure">
              <strong>{data.diagnostic.error_type}</strong>
              <p>{data.diagnostic.error_message}</p>
              <pre>{data.diagnostic.stacktrace}</pre>
              {data.diagnostic.truncated && (
                <p>
                  {zh ? "异常详情已截断。" : "Exception details are truncated."}
                </p>
              )}
            </div>
          )}
          {data.context && (
            <p className="observe-note">
              {zh
                ? "当前展示同一进程的相邻日志；“匹配”行包含此 Trace ID，其余行作为时间上下文。"
                : "Showing nearby logs from the same process. Matched lines contain this Trace ID; other lines provide time context."}
            </p>
          )}
          {data.possibly_truncated && (
            <p className="observe-note">
              {zh
                ? "日志达到读取上限，当前展示部分结果。"
                : "Log read limit reached; results are partial."}
            </p>
          )}
          <div className="observe-logs">
            {rows.map((row, i) => (
              <article
                key={`${row.timestamp}:${i}`}
                className={row.matched ? "matched" : ""}
              >
                <header>
                  <time>{timeText(row.timestamp, zh)}</time>
                  <span>
                    {row.task} · {row.stream}
                  </span>
                  {row.matched && <span>{zh ? "匹配" : "Matched"}</span>}
                </header>
                <pre>{row.line}</pre>
              </article>
            ))}
          </div>
          {!rows.length && (
            <ReadState
              zh={zh}
              empty={
                zh
                  ? "当前范围没有匹配日志。"
                  : "No matching logs in this window."
              }
            />
          )}
        </>
      )}
    </section>
  );
}

export function TracePanel({
  app,
  token,
  zh,
  traceId,
  hours,
  until,
  onTrace,
}: BoardProps & {
  traceId: string;
  hours: number;
  until?: number;
  onTrace: (id: string) => void;
}) {
  const [revision, setRevision] = useState(0);
  const { path, search, navigate } = useRouter();
  const selected = new URLSearchParams(search).get("observeSpan") || "";
  const setSelected = (id: string) =>
    navigate(investigationPath(path, search, { observeSpan: id }), {
      replace: true,
    });
  const [filter, setFilter] = useState("");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [zoom, setZoom] = useState("1");
  const [showLogs, setShowLogs] = useState(false);
  const [copied, setCopied] = useState(false);
  const { data, loading, error } = useObservation<TraceDetail>(
    "trace",
    app,
    token,
    { trace_id: traceId },
    revision,
  );
  const model = useMemo(() => buildTrace(data?.spans || []), [data]);
  const selectedRow =
    model.rows.find((row) => row.span.spanId === selected) ||
    model.rows.find((row) => row.span.status === "error") ||
    model.rows[0];
  const matches = model.rows.filter(
    (row) =>
      (!filter ||
        `${row.span.name} ${row.span.service}`
          .toLowerCase()
          .includes(filter.toLowerCase())) &&
      (!errorsOnly || row.span.status === "error"),
  );
  const included = new Set(
    matches.flatMap((row) => [row.span.spanId, ...row.ancestors]),
  );
  const rows = model.rows.filter(
    (row) =>
      included.has(row.span.spanId) &&
      (filter || errorsOnly || !row.ancestors.some((id) => collapsed.has(id))),
  );
  const span = selectedRow?.span;
  const panes = {
    trace: {
      datasource: "tempo",
      queries: [{ refId: "A", queryType: "traceql", query: traceId }],
      range: grafanaRange(search),
    },
  };
  const explore =
    "/grafana/explore?schemaVersion=1&panes=" +
    encodeURIComponent(JSON.stringify(panes));
  const toggle = (id: string) =>
    setCollapsed((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  return (
    <section className="observe-trace">
      <header className="observe-detail-heading">
        <div>
          <span className="observe-note">Trace ID</span>
          <code>{traceId}</code>
        </div>
        <div className="observe-actions">
          <Button
            variant="ghost"
            size="sm"
            onClick={() =>
              void navigator.clipboard
                .writeText(traceId)
                .then(() => setCopied(true))
                .catch(() => setCopied(false))
            }
          >
            {copied ? (zh ? "已复制" : "Copied") : zh ? "复制" : "Copy"}
          </Button>
          <a
            className={buttonVariants({ variant: "outline", size: "sm" })}
            href={explore}
            target="_blank"
            rel="noopener noreferrer"
          >
            Grafana
            <ArrowUpRight data-icon="inline-end" />
          </a>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setRevision((n) => n + 1)}
            disabled={loading}
          >
            {zh ? "刷新" : "Refresh"}
          </Button>
        </div>
      </header>
      <ReadState
        loading={loading}
        state={error || data?.state}
        zh={zh}
        retry={() => setRevision((n) => n + 1)}
      />
      {!loading && data?.state === "ok" && (
        <>
          <div className="observe-summary">
            <span>
              {spanDuration(model.total)}{" "}
              <small>{zh ? "时间跨度" : "elapsed"}</small>
            </span>
            <span>
              {model.rows.length} <small>Spans</small>
            </span>
            <span>
              {new Set(model.rows.map((r) => r.span.service)).size}{" "}
              <small>{zh ? "服务" : "services"}</small>
            </span>
            <span>
              {model.rows.filter((r) => r.span.status === "error").length}{" "}
              <small>{zh ? "错误 Span" : "error spans"}</small>
            </span>
          </div>
          {(data.partial || data.truncated) && (
            <p className="observe-note">
              {zh
                ? "当前为部分调用链：范围外或超过读取上限的 Span 已省略。"
                : "Partial trace: spans outside this application or beyond the read limit are omitted."}
            </p>
          )}
          <div className="observe-toolbar">
            <Input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              aria-label={zh ? "筛选 Span" : "Filter spans"}
              placeholder={zh ? "查找调用或服务" : "Find operation or service"}
            />
            <Button
              variant={errorsOnly ? "secondary" : "outline"}
              size="sm"
              aria-pressed={errorsOnly}
              onClick={() => setErrorsOnly((v) => !v)}
            >
              {zh ? "仅错误" : "Errors only"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setCollapsed(new Set())}
            >
              {zh ? "展开全部" : "Expand all"}
            </Button>
            <SelectControl
              ariaLabel={zh ? "时间轴缩放" : "Timeline zoom"}
              className="observe-zoom"
              value={zoom}
              onChange={setZoom}
              options={["1", "2", "4"].map((v) => ({
                value: v,
                label: `${v}×`,
              }))}
            />
          </div>
          <div className="observe-waterfall-scroll">
            <table
              className="observe-waterfall"
              style={{ minWidth: `${Math.max(760, 760 * Number(zoom))}px` }}
            >
              <thead>
                <tr>
                  <th>{zh ? "调用 / 服务" : "Operation / service"}</th>
                  <th>
                    <div className="observe-ruler">
                      {[0, 1, 2, 3, 4].map((i) => (
                        <span key={i}>
                          {spanDuration((model.total * i) / 4)}
                        </span>
                      ))}
                    </div>
                  </th>
                  <th>{zh ? "耗时" : "Duration"}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.span.spanId}
                    data-selected={span?.spanId === row.span.spanId}
                  >
                    <td>
                      <div
                        className="observe-span-name"
                        style={{
                          paddingInlineStart: `${Math.min(row.depth, 15) * 12}px`,
                        }}
                      >
                        <button
                          className="observe-collapse"
                          aria-label={`${collapsed.has(row.span.spanId) ? (zh ? "展开" : "Expand") : zh ? "折叠" : "Collapse"} ${row.span.name}`}
                          aria-expanded={!collapsed.has(row.span.spanId)}
                          disabled={!row.children.length}
                          onClick={() => toggle(row.span.spanId)}
                        >
                          {row.children.length ? (
                            collapsed.has(row.span.spanId) ? (
                              <ChevronRightIcon className="size-3" />
                            ) : (
                              <ChevronDown className="size-3" />
                            )
                          ) : (
                            "·"
                          )}
                        </button>
                        <button
                          onClick={() => setSelected(row.span.spanId)}
                          title={row.span.name}
                        >
                          <strong>{row.span.name}</strong>
                          <small>
                            {row.span.service}
                            {row.orphan
                              ? zh
                                ? " · 父 Span 未保留"
                                : " · parent unavailable"
                              : ""}
                          </small>
                        </button>
                      </div>
                    </td>
                    <td>
                      <button
                        className="observe-timeline"
                        aria-label={`${row.span.name} ${spanDuration(row.duration)}`}
                        onClick={() => setSelected(row.span.spanId)}
                      >
                        <span
                          className={row.span.status === "error" ? "error" : ""}
                          style={{
                            left: `${(row.offset / (model.total || 1)) * 100}%`,
                            width: `${Math.max(0.2, (row.duration / (model.total || 1)) * 100)}%`,
                          }}
                        />
                      </button>
                    </td>
                    <td className="observe-numeric">
                      {spanDuration(row.duration)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!rows.length && (
            <ReadState
              zh={zh}
              empty={zh ? "没有匹配的 Span。" : "No matching spans."}
            />
          )}
          {span && selectedRow && (
            <section className="observe-span-detail">
              <header>
                <h3>{span.name}</h3>
                <Outcome value={span.status} zh={zh} />
                <span>
                  {zh ? "自身耗时" : "Self time"}{" "}
                  {spanDuration(selectedRow.self)}
                </span>
              </header>
              <AttributeTable
                values={{
                  "span.id": span.spanId,
                  "parent.id": span.parentSpanId || "—",
                  service: span.service,
                  duration: spanDuration(selectedRow.duration),
                }}
              />
              <details open>
                <summary>
                  {zh ? "属性" : "Attributes"} (
                  {Object.keys(span.attributes).length})
                </summary>
                <AttributeTable values={span.attributes} />
              </details>
              <details>
                <summary>{zh ? "资源" : "Resource"}</summary>
                <AttributeTable values={span.resource} />
              </details>
              <details open={span.events.length > 0}>
                <summary>
                  {zh ? "事件" : "Events"} ({span.events.length})
                </summary>
                {span.events.map((event, i) => (
                  <div key={i} className="observe-span-event">
                    <strong>{event.name}</strong>
                    <span>
                      +
                      {spanDuration(
                        milliseconds(
                          event.timeUnixNano,
                          span.startTimeUnixNano,
                        ),
                      )}
                    </span>
                    <AttributeTable values={event.attributes} />
                  </div>
                ))}
              </details>
              {span.links.length > 0 && (
                <div className="observe-span-links">
                  <span>{zh ? "关联 Trace" : "Linked traces"}</span>
                  {span.links.map((link) => (
                    <Button
                      key={`${link.traceId}:${link.spanId}`}
                      variant="outline"
                      size="sm"
                      onClick={() => onTrace(link.traceId)}
                    >
                      <code>{link.traceId}</code>
                      <ArrowUpRight data-icon="inline-end" />
                    </Button>
                  ))}
                </div>
              )}
              {(span.detailsTruncated || span.droppedAttributes > 0) && (
                <p className="observe-note">
                  {zh
                    ? "部分属性或事件已截断或在采集时丢弃。"
                    : "Some attributes or events were truncated or dropped during collection."}
                </p>
              )}
            </section>
          )}
        </>
      )}
      <Button
        className="mt-4"
        variant="outline"
        size="sm"
        aria-expanded={showLogs}
        onClick={() => setShowLogs((v) => !v)}
      >
        {showLogs
          ? zh
            ? "收起关联日志"
            : "Hide related logs"
          : zh
            ? "错误堆栈与关联日志"
            : "Exception and related logs"}
      </Button>
      {showLogs && (
        <TraceLogs
          app={app}
          token={token}
          zh={zh}
          traceId={traceId}
          hours={hours}
          until={until}
        />
      )}
      <p className="observe-note mt-3">
        {zh
          ? "Trace 展示已采样、仍在保留期内的调用。任务执行数量以指标为准。"
          : "Traces show sampled, retained calls. Use metrics for execution counts."}
      </p>
    </section>
  );
}
