import { observationQuery } from "../../observeContext";
import { ArrowLeft } from "@/components/icons";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";
import { useState, useMemo, type ReactNode } from "react";
import { useRouter } from "../../router";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Card, CardContent } from "@/components/ui/card";
import { SelectControl } from "../primitives";
import { TaskOverview } from "./TaskOverview";
import { ExecutionBrowser } from "./ExecutionBrowser";
import { TracePanel } from "./TracePanel";
import { ReadState, durationText, timeText, useObservation } from "./shared";
import type { BoardProps, Query, TracesResult } from "./types";
import "./task-observability.css";

type Filters = {
  task_name: string;
  task_id: string;
  queue: string;
  outcome: string;
  attribute: string;
  value: string;
  min_duration_ms: string;
};
function QueryFilters({
  query,
  zh,
  traces,
  onApply,
}: {
  query: Query;
  zh: boolean;
  traces: boolean;
  onApply: (values: Filters) => void;
}) {
  const [values, setValues] = useState<Filters>({
    task_name: String(query.task_name || ""),
    task_id: String(query.task_id || ""),
    queue: String(query.queue || ""),
    outcome: String(query.outcome || "all"),
    attribute: String(query.attribute || ""),
    value: String(query.value || ""),
    min_duration_ms: String(query.min_duration_ms || ""),
  });
  const input = (key: keyof Filters, label: string, type = "text") => (
    <Input
      value={values[key]}
      type={type}
      min={type === "number" ? 0 : undefined}
      max={type === "number" ? 259200000 : undefined}
      maxLength={
        key === "value"
          ? 256
          : key === "task_id" || key === "attribute"
            ? 100
            : 160
      }
      aria-label={label}
      placeholder={label}
      required={
        key === "attribute"
          ? Boolean(values.value)
          : key === "value"
            ? Boolean(values.attribute)
            : false
      }
      pattern={key === "attribute" ? "[A-Za-z_][A-Za-z0-9_.\\-]*" : undefined}
      onChange={(e) => setValues((v) => ({ ...v, [key]: e.target.value }))}
    />
  );
  return (
    <form
      className="observe-filters"
      onSubmit={(e) => {
        e.preventDefault();
        onApply(values);
      }}
    >
      <div className="observe-toolbar">
        {input("task_name", zh ? "任务名称（精确）" : "Exact task name")}
        {input("task_id", zh ? "任务编号" : "Task ID")}
        {input("queue", zh ? "队列" : "Queue")}
        <SelectControl
          value={values.outcome}
          onChange={(outcome) => setValues((v) => ({ ...v, outcome }))}
          ariaLabel={zh ? "执行结果" : "Outcome"}
          options={[
            { value: "all", label: zh ? "全部结果" : "All outcomes" },
            {
              value: "problem",
              label: zh ? "失败与重试" : "Failures and retries",
            },
          ]}
        />
        {traces &&
          input(
            "min_duration_ms",
            zh ? "最小耗时 ms" : "Min duration ms",
            "number",
          )}
        <Button type="submit" size="sm">
          {zh ? "查询" : "Search"}
        </Button>
      </div>
      <details>
        <summary>
          {zh ? "按已上报的属性筛选" : "Filter by reported attributes"}
        </summary>
        <div className="observe-toolbar">
          {input(
            "attribute",
            zh ? "属性名，例如 order.id" : "Attribute, e.g. order.id",
          )}
          {input("value", zh ? "属性值" : "Attribute value")}
          <span className="observe-note">
            {zh
              ? "可查询遥测中的业务编号。"
              : "Search business references included in telemetry."}
          </span>
        </div>
      </details>
    </form>
  );
}

function TraceSearch({
  app,
  token,
  zh,
  query,
  onTrace,
}: BoardProps & { query: Query; onTrace: (id: string) => void }) {
  const [revision, setRevision] = useState(0);
  const { data, loading, error } = useObservation<TracesResult>(
    "traces",
    app,
    token,
    query,
    revision,
  );
  return (
    <section>
      <div className="observe-toolbar">
        <p className="observe-note">
          {zh
            ? "查询已采样的调用，最多返回 50 条；任务执行全集请查看执行记录。"
            : "Search sampled calls, up to 50 results. Use execution records for task lifecycle evidence."}
        </p>
        <Button
          variant="outline"
          size="sm"
          disabled={loading}
          onClick={() => setRevision((n) => n + 1)}
        >
          {zh ? "刷新" : "Refresh"}
        </Button>
      </div>
      <ReadState
        zh={zh}
        loading={loading}
        state={error || data?.state}
        retry={() => setRevision((n) => n + 1)}
      />
      {!loading && data?.state === "ok" && (
        <>
          <Table containerProps={{ className: "observe-edge-table" }}>
            <TableHeader>
              <TableRow>
                {(zh
                  ? ["调用 / Trace ID", "根服务", "开始时间", "耗时"]
                  : [
                      "Operation / Trace ID",
                      "Root service",
                      "Started",
                      "Duration",
                    ]
                ).map((label) => (
                  <TableHead key={label}>{label}</TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.items.map((trace) => (
                <TableRow key={trace.traceID}>
                  <TableCell>
                    <button
                      className="observe-task-link"
                      onClick={() => onTrace(trace.traceID)}
                    >
                      <strong>{trace.rootTraceName || trace.traceID}</strong>
                      <small>{trace.traceID}</small>
                    </button>
                  </TableCell>
                  <TableCell>{trace.rootServiceName || "—"}</TableCell>
                  <TableCell>
                    {timeText(Number(trace.startTimeUnixNano) / 1e9, zh)}
                  </TableCell>
                  <TableCell className="observe-numeric">
                    {durationText(trace.durationMs / 1000)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {!data.items.length && (
            <ReadState
              zh={zh}
              empty={
                zh
                  ? "当前范围没有保留的 Trace。可扩大范围，或直接输入 Trace ID。"
                  : "No retained traces in this window. Widen the range or enter a Trace ID."
              }
            />
          )}
          {data.possibly_truncated && (
            <p className="observe-note">
              {zh
                ? "结果达到上限，请缩小时间范围或增加筛选条件。"
                : "Result limit reached. Narrow the window or add filters."}
            </p>
          )}
        </>
      )}
    </section>
  );
}

export function ApplicationTaskBoard({
  app,
  token,
  zh,
  metrics,
}: BoardProps & { metrics: ReactNode }) {
  const { path, search, navigate } = useRouter();
  const params = new URLSearchParams(search);
  const rawView = params.get("observeView") || "metrics";
  const view = ["metrics", "overview", "events", "traces"].includes(rawView)
    ? rawView
    : "metrics";
  const hours = [1, 6, 24, 72].includes(Number(params.get("observeHours")))
    ? Number(params.get("observeHours"))
    : 1;
  const rawId = params.get("observeTrace") || "";
  const traceId = /^[0-9a-f]{32}$/i.test(rawId) ? rawId.toLowerCase() : "";
  const [lookup, setLookup] = useState("");
  const context = useMemo(() => observationQuery(search), [search]);
  const query: Query = { ...context, outcome: "all" };
  for (const key of [
    "task_name",
    "task_id",
    "queue",
    "outcome",
    "attribute",
    "value",
    "min_duration_ms",
    "until",
  ]) {
    const value = params.get(`observe_${key}`);
    if (value) query[key] = value;
  }
  const change = (values: Record<string, string | number | undefined>) => {
    const next = new URLSearchParams(search);
    for (const [key, value] of Object.entries(values)) {
      if (value === undefined || value === "") next.delete(key);
      else next.set(key, String(value));
    }
    navigate(`${path}?${next}`);
  };
  const onTrace = (id: string, until?: number) =>
    change({
      observeTrace: id,
      observeSpan: "",
      ...(until ? { observe_until: until } : {}),
    });
  const props = { app, token, zh };
  const filtersKey = JSON.stringify(query);
  return (
    <div className="task-observation">
      <Tabs
        value={view}
        onValueChange={(value) =>
          change({ observeView: String(value), observeTrace: undefined })
        }
      >
        <div className="observe-board-nav">
          <TabsList aria-label={zh ? "看板视图" : "Dashboard view"}>
            {[
              ["metrics", "指标看板", "Metrics"],
              ["overview", "任务总览", "Tasks"],
              ["events", "执行记录", "Executions"],
              ["traces", "Trace", "Traces"],
            ].map(([id, cn, en]) => (
              <TabsTrigger key={id} value={id}>
                {zh ? cn : en}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
        <TabsContent value="metrics">
          {view === "metrics" && metrics}
        </TabsContent>
        {view !== "metrics" && (
          <Card>
            <CardContent>
              <TabsContent value={view}>
                {traceId ? (
                  <>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="mb-3"
                      onClick={() => change({ observeTrace: undefined })}
                    >
                      <ArrowLeft data-icon="inline-start" />
                      {zh ? "返回查询结果" : "Back to results"}
                    </Button>
                    <TracePanel
                      key={traceId}
                      {...props}
                      traceId={traceId}
                      hours={hours}
                      until={query.until ? Number(query.until) : undefined}
                      onTrace={onTrace}
                    />
                  </>
                ) : view === "overview" ? (
                  <TaskOverview
                    {...props}
                    hours={hours}
                    query={query}
                    onTask={(name, queue) =>
                      change({
                        observeView: "events",
                        observe_task_name: name,
                        observe_queue: queue,
                        observe_task_id: undefined,
                        observe_outcome: "all",

                        observe_attribute: undefined,
                        observe_value: undefined,
                        observe_min_duration_ms: undefined,
                      })
                    }
                  />
                ) : (
                  <>
                    {query.task_id && view === "events" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="mb-3"
                        onClick={() =>
                          change({
                            observe_task_id: undefined,
                          })
                        }
                      >
                        <ArrowLeft data-icon="inline-start" />
                        {zh ? "返回任务列表" : "Back to task list"}
                      </Button>
                    )}
                    {!(query.task_id && view === "events") && (
                      <QueryFilters
                        key={`filters:${filtersKey}`}
                        query={query}
                        zh={zh}
                        traces={view === "traces"}
                        onApply={(values) =>
                          change({
                            ...Object.fromEntries(
                              Object.entries(values).map(([key, value]) => [
                                `observe_${key}`,
                                value,
                              ]),
                            ),
                          })
                        }
                      />
                    )}
                    {view === "traces" && (
                      <form
                        className="observe-toolbar observe-trace-lookup"
                        onSubmit={(e) => {
                          e.preventDefault();
                          onTrace(lookup.toLowerCase());
                          setLookup("");
                        }}
                      >
                        <Input
                          aria-label="Trace ID"
                          placeholder={
                            zh ? "直接输入 Trace ID" : "Enter a Trace ID"
                          }
                          pattern="[0-9a-fA-F]{32}"
                          required
                          maxLength={32}
                          value={lookup}
                          onChange={(e) => setLookup(e.target.value.trim())}
                        />
                        <Button size="sm" variant="outline" type="submit">
                          {zh ? "打开 Trace" : "Open trace"}
                        </Button>
                      </form>
                    )}
                    {view === "events" ? (
                      <ExecutionBrowser
                        key={`events:${filtersKey}`}
                        {...props}
                        query={query}
                        onTask={(id, until) =>
                          change({
                            observe_task_id: id,
                            observe_outcome: "all",
                            observe_until: until,
                          })
                        }
                        onTrace={onTrace}
                      />
                    ) : (
                      <TraceSearch {...props} query={query} onTrace={onTrace} />
                    )}
                  </>
                )}
              </TabsContent>
            </CardContent>
          </Card>
        )}
      </Tabs>
    </div>
  );
}
