import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StatePill } from "../primitives";
import {
  ReadState,
  durationText,
  sourceText,
  timeText,
  useObservation,
} from "./shared";
import { overviewSources, processMetrics, taskMetrics } from "./overviewModel";
import { OverviewProgress } from "./OverviewProgress";
import type { BoardProps, OverviewResult, Query } from "./types";

export function TaskOverview({
  app,
  token,
  zh,
  hours,
  query,
  onTask,
}: BoardProps & {
  hours: number;
  query?: Query;
  onTask: (name: string, queue: string) => void;
}) {
  const [revision, setRevision] = useState(0);
  const [search, setSearch] = useState("");
  const [limit, setLimit] = useState(50);
  const [automatic, setAutomatic] = useState(false);
  const [activeOnly, setActiveOnly] = useState(false);
  const { data, loading, error } = useObservation<OverviewResult>(
    "overview",
    app,
    token,
    query || { hours },
    revision,
  );
  useEffect(() => {
    if (!automatic || loading) return;
    const timer = window.setTimeout(() => {
      if (document.visibilityState === "visible") setRevision((n) => n + 1);
    }, 30000);
    const visible = () => {
      if (document.visibilityState === "visible") setRevision((n) => n + 1);
    };
    document.addEventListener("visibilitychange", visible);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [automatic, loading, revision]);
  const sources = data?.sources || {};
  const tasks = taskMetrics(sources);
  const shown = tasks.filter((task) =>
    (!activeOnly || (task.active || 0) > 0) && `${task.name} ${task.queue} ${task.runtime}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const { unavailable: failures, stale, empty } = overviewSources(sources);
  const sourceLabels: Record<string, [string, string]> = {
    active: ["活跃任务", "Activity"], oldest: ["最长执行", "Longest execution"],
    no_progress: ["进展更新", "Progress"], executions: ["完成记录", "Completions"],
    span_executions: ["完成记录", "Completions"], duration: ["执行耗时", "Duration"],
    span_duration: ["执行耗时", "Duration"], wait: ["排队 P95", "Queue P95"],
    last_finished: ["最近完成", "Last completion"], processes: ["进程心跳", "Heartbeats"],
    registered: ["任务登记", "Registered tasks"], drops: ["SDK 记录", "SDK records"],
  };
  const sourceLabel = (key: string) => sourceLabels[key]?.[zh ? 0 : 1] || key;
  const processes = processMetrics(sources, data?.sampled_at || 0);
  const count = (value?: number) =>
    value == null
      ? "—"
      : value.toLocaleString(zh ? "zh-CN" : "en-GB", {
          maximumFractionDigits: 1,
        });
  return (
    <section>
      <div className="observe-toolbar">
        <Input
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setLimit(50);
          }}
          aria-label={zh ? "筛选任务" : "Filter tasks"}
          placeholder={
            zh ? "搜索任务、队列或框架" : "Search task, queue or framework"
          }
        />
        <Button
          size="sm"
          variant={automatic ? "secondary" : "outline"}
          aria-pressed={automatic}
          onClick={() => setAutomatic((v) => !v)}
        >
          {zh ? "自动刷新 · 30s" : "Auto refresh · 30s"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => setRevision((n) => n + 1)}
          disabled={loading}
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
          {failures.length > 0 && (
            <div className="observe-source-errors" role="status">
              {zh
                ? "部分来源不可用，相关值保留为未知："
                : "Some sources are unavailable; affected values remain unknown: "}
              {failures.map(([key, source]) => (
                <span key={key}>
                  {sourceLabel(key)} · {sourceText(source.state, zh)}
                </span>
              ))}
            </div>
          )}
          {stale.length > 0 && (
            <p className="observe-source-errors" role="status">
              {zh ? "以下数据已过期，不能代表当前状态：" : "These samples are stale and do not represent current state: "}
              {stale.map(([key]) => sourceLabel(key)).join(" · ")}
            </p>
          )}
          <OverviewProgress sources={sources} tasks={tasks} sampledAt={data.sampled_at || 0} zh={zh} onTask={onTask} />
          {empty.some(([key]) => key !== "executions" && key !== "span_executions") && (
            <p className="observe-note">
              {zh ? "暂无样本：" : "No samples: "}
              {empty.filter(([key]) => key !== "executions" && key !== "span_executions").map(([key]) => sourceLabel(key)).join(" · ")}
            </p>
          )}
          <div className="observe-table-heading">
            <div>
              <h3>{zh ? "任务明细" : "Task details"} <span>{shown.length}</span></h3>
              <p className="observe-note">{zh ? "活跃任务优先，按未上报进展时长排序。排队 P95 来自已开始执行的任务。" : "Active tasks first, ordered by time without reported progress. Queue P95 describes tasks that have started."}</p>
            </div>
            <Button size="sm" variant={activeOnly ? "secondary" : "outline"} aria-pressed={activeOnly}
              onClick={() => { setActiveOnly((value) => !value); setLimit(50); }}>
              {zh ? "仅看活跃任务" : "Active tasks only"}
            </Button>
          </div>
          <Table containerProps={{ className: "observe-edge-table" }}>
            <TableHeader>
              <TableRow>
                {(zh
                  ? [
                      "任务 / 队列",
                      "框架",
                      "活跃",
                      "成功 / 失败 / 重试 / 取消 / 未知",
                      "耗时 P95",
                      "排队 P95",
                      "最长执行 / 进展未更新",
                      "最近完成",
                    ]
                  : [
                      "Task / queue",
                      "Framework",
                      "Active",
                      "Success / failure / retry / cancel / unknown",
                      "Duration P95",
                      "Queue P95",
                      "Longest / no reported progress",
                      "Last finished",
                    ]
                ).map((text) => (
                  <TableHead key={text}>{text}</TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {shown.slice(0, limit).map((task) => (
                <TableRow key={task.key} data-active={(task.active || 0) > 0 ? "true" : undefined}>
                  <TableCell>
                    <button
                      className="observe-task-link"
                      onClick={() => onTask(task.name, task.queue)}
                    >
                      <strong>{task.name}</strong>
                      <small>{task.queue || "—"}</small>
                    </button>
                  </TableCell>
                  <TableCell>{task.runtime}</TableCell>
                  <TableCell className="observe-numeric">
                    {count(task.active)}
                  </TableCell>
                  <TableCell className="observe-numeric">
                    {count(task.results.succeeded)} /{" "}
                    <span
                      className={
                        task.results.failed > 0 ? "observe-error-text" : ""
                      }
                    >
                      {count(task.results.failed)}
                    </span>{" "}
                    / {count(task.results.retrying)} / {count(task.results.cancelled)} / {count(task.results.unknown)}
                  </TableCell>
                  <TableCell className="observe-numeric">
                    {durationText(task.duration)}
                  </TableCell>
                  <TableCell className="observe-numeric">
                    {durationText(task.wait)}
                  </TableCell>
                  <TableCell className="observe-numeric">
                    <div className="observe-duration-pair">
                      <span>{durationText(task.oldest)} <small>{zh ? "执行" : "running"}</small></span>
                      <span>{durationText(task.no_progress)} <small>{zh ? "进展未更新" : "since progress"}</small></span>
                    </div>
                  </TableCell>
                  <TableCell>{timeText(task.last, zh)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {!shown.length && (
            <ReadState
              zh={zh}
              empty={
                !tasks.length && failures.length
                  ? zh
                    ? "当前没有可显示的任务数据，请先恢复观测查询。"
                    : "No task data is available. Restore the observation queries first."
                  : activeOnly
                    ? zh ? "当前筛选下没有已观测到的活跃任务。" : "No observed active tasks match this filter."
                  : tasks.length
                    ? zh
                      ? "没有匹配的任务。"
                      : "No matching tasks."
                    : zh
                      ? "此应用当前没有任务指标。接入 SDK 并上报任务后即可查看；Trace 页面也支持其他 OpenTelemetry 调用。"
                      : "No task metrics for this application. Instrument tasks with the SDK to populate this view; Traces also supports other OpenTelemetry calls."
              }
            />
          )}
          {shown.length > limit && (
            <Button
              size="sm"
              variant="outline"
              className="mt-3"
              onClick={() => setLimit((n) => n + 50)}
            >
              {zh ? "显示更多任务" : "Show more tasks"}
            </Button>
          )}
          <p className="observe-note mt-3">
            {zh
              ? "结果来自所选时段的完成记录；旧版指标来源可能为估算。— 表示缺少可用样本。活跃与进展只计最近 120 秒有心跳的进程；部分进程缺失时，活跃数仅代表已观测数量。Celery 完成 Span 不提供实时活跃数。"
              : "Results use completion records in the selected range; older metric sources may be estimates. — means no usable samples. Activity and progress include processes reporting within 120 seconds; missing processes can make activity incomplete. Celery completion spans do not provide live activity."}
          </p>
          <details className="observe-processes">
            <summary>
              {zh ? "进程与采集状态" : "Processes and collection"} (
              {processes.length})
            </summary>
            <Table containerProps={{ className: "observe-edge-table" }}>
              <TableHeader>
                <TableRow>
                  {(zh
                    ? ["服务 / 实例", "角色 / 队列", "心跳距今", "启动时间"]
                    : [
                        "Service / instance",
                        "Role / queue",
                        "Heartbeat age",
                        "Started",
                      ]
                  ).map((label) => (
                    <TableHead key={label}>{label}</TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {processes.map((process) => (
                  <TableRow key={process.id}>
                    <TableCell>
                      {process.service}
                      <small className="block text-muted-foreground">
                        {process.id}
                      </small>
                    </TableCell>
                    <TableCell>
                      {process.role || "—"}
                      <small className="block text-muted-foreground">
                        {process.queue}
                      </small>
                    </TableCell>
                    <TableCell>
                      <StatePill
                        value={
                          process.age == null
                            ? "unknown"
                            : process.age < 120
                              ? "ready"
                              : "warning"
                        }
                        label={durationText(process.age)}
                      />
                    </TableCell>
                    <TableCell>{timeText(process.started, zh)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {sources.drops?.state === "ok" && sources.drops.rows.length > 0 && (
              <div className="observe-summary">
                {sources.drops.rows.map((row, i) => (
                  <span key={i}>
                    {row.metric.reason || "SDK"}: {count(Number(row.value[1]))}
                  </span>
                ))}
                <small>{zh ? "SDK 丢弃计数" : "SDK drop counters"}</small>
              </div>
            )}
          </details>
        </>
      )}
    </section>
  );
}
