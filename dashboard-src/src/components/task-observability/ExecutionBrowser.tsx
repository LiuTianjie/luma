import { ArrowUpRight } from "@/components/icons";
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
import {
  AttributeTable,
  durationText,
  Outcome,
  ReadState,
  timeText,
  useObservation,
} from "./shared";
import { executionPhases, executions, mergeEvents } from "./executionModel";
import { TraceLogs } from "./TracePanel";
import type { BoardProps, EventsResult, Query, TaskEvent } from "./types";

export function ExecutionBrowser({
  app,
  token,
  zh,
  query,
  onTask,
  onTrace,
}: BoardProps & {
  query: Query;
  onTask: (taskId: string, until?: number) => void;
  onTrace: (traceId: string, until?: number) => void;
}) {
  const [page, setPage] = useState<{
    offset: number;
    until?: number;
    revision: number;
  }>({ offset: 0, revision: 0 });
  const [loaded, setLoaded] = useState<TaskEvent[]>([]);
  const [selectedAttempt, setSelectedAttempt] = useState("");
  const [showLogs, setShowLogs] = useState(false);
  const params = {
    ...query,
    offset: page.offset,
    ...(page.until ? { until: page.until } : {}),
  };
  const { data, loading, error } = useObservation<EventsResult>(
    "events",
    app,
    token,
    params,
    page.revision,
  );
  useEffect(() => {
    if (data?.state === "ok")
      setLoaded((old) =>
        page.offset ? mergeEvents(old, data.items) : data.items,
      );
  }, [data, page.offset]);
  const groups = executions(loaded);
  const group = query.task_id
    ? groups.find((item) => item.id === query.task_id)
    : undefined;
  const attempt =
    group?.attempts.find((item) => item.id === selectedAttempt) ||
    group?.latest;
  const phases = executionPhases(attempt?.events || []);
  const longest = Math.max(1, ...phases.map((phase) => phase.durationMs || 0));
  const omitted = Math.max(
    0,
    ...(attempt?.events || []).map((event) => event.phase_omitted || 0),
  );
  const refresh = () => {
    setLoaded([]);
    setPage((p) => ({ offset: 0, revision: p.revision + 1 }));
  };
  return (
    <section>
      {!query.task_id && (
        <div className="observe-toolbar">
          <p className="observe-note">
            {zh
              ? `已读取 ${groups.length} 个任务 · ${loaded.length} 条事件`
              : `${groups.length} loaded tasks · ${loaded.length} events`}
          </p>
          <Button
            className="ml-auto"
            variant="outline"
            size="sm"
            disabled={loading}
            onClick={refresh}
          >
            {zh ? "刷新记录" : "Refresh records"}
          </Button>
        </div>
      )}
      <ReadState
        zh={zh}
        loading={loading}
        state={error || data?.state}
        retry={refresh}
      />
      {query.outcome === "problem" && (
        <p className="observe-note mb-3">
          {zh
            ? "这里列出曾失败或重试的执行；打开任务后可核对后续结果。"
            : "These executions failed or retried. Open a task to inspect its subsequent outcome."}
        </p>
      )}
      {!query.task_id && groups.length > 0 && (
        <Table containerProps={{ className: "observe-edge-table" }}>
          <TableHeader>
            <TableRow>
              {(zh
                ? ["任务 / 编号", "最新已观测结果", "尝试", "耗时", "最近事件"]
                : [
                    "Task / ID",
                    "Latest observed outcome",
                    "Attempts",
                    "Duration",
                    "Latest event",
                  ]
              ).map((text) => (
                <TableHead key={text}>{text}</TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {groups.map((item) => (
              <TableRow key={item.id}>
                <TableCell>
                  <button
                    className="observe-task-link"
                    onClick={() =>
                      item.latest.taskId
                        ? onTask(item.latest.taskId, data?.until)
                        : onTrace(item.latest.traceId, data?.until)
                    }
                  >
                    <strong>{item.latest.taskName || item.id}</strong>
                    <small>{item.latest.taskId || item.latest.traceId}</small>
                    {item.latest.error && (
                      <span className="observe-error-text">
                        {item.latest.error.stage} ·{" "}
                        {item.latest.error.error_message ||
                          item.latest.error.error_type}
                      </span>
                    )}
                  </button>
                </TableCell>
                <TableCell>
                  <Outcome value={item.outcome} zh={zh} />
                </TableCell>
                <TableCell>{item.observedAttempts}</TableCell>
                <TableCell className="observe-numeric">
                  {durationText(item.latest.duration)}
                </TableCell>
                <TableCell>{timeText(item.latest.timestamp, zh)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {group && attempt && (
        <>
          <header className="observe-detail-heading">
            <div>
              <h3>{attempt.taskName}</h3>
              <code>{group.id}</code>
            </div>
            <div className="observe-actions">
              <Outcome value={group.outcome} zh={zh} />
              <Button
                variant="ghost"
                size="sm"
                disabled={loading}
                onClick={refresh}
              >
                {zh ? "重新读取" : "Reload"}
              </Button>
            </div>
          </header>
          <div
            className="observe-attempts"
            aria-label={zh ? "执行尝试" : "Execution attempts"}
          >
            {group.attempts.map((item, index) => (
              <button
                key={item.id}
                aria-pressed={item.id === attempt.id}
                onClick={() => {
                  setSelectedAttempt(item.id);
                  setShowLogs(false);
                }}
              >
                <small>
                  {item.retry != null
                    ? zh
                      ? `第 ${item.retry + 1} 次尝试`
                      : `Attempt ${item.retry + 1}`
                    : zh
                      ? `记录 ${index + 1}`
                      : `Record ${index + 1}`}
                </small>
                <Outcome value={item.outcome} zh={zh} />
                <span>{durationText(item.duration)}</span>
              </button>
            ))}
          </div>
          {attempt.error && (
            <div className="observe-failure">
              <header>
                <strong>
                  {zh ? "失败阶段" : "Failure stage"} ·{" "}
                  {attempt.error.stage || "—"}
                </strong>
              </header>
              <p>
                {attempt.error.error_type}: {attempt.error.error_message}
              </p>
              {attempt.error.error_location && (
                <code>{attempt.error.error_location}</code>
              )}
              {attempt.error.error_truncated && (
                <small>
                  {zh
                    ? "摘要已截断，可读取原始异常。"
                    : "Summary truncated; load the original exception for details."}
                </small>
              )}
            </div>
          )}
          <section className="observe-phases">
            <header>
              <h3>{zh ? "阶段耗时" : "Phase durations"}</h3>
              <span>
                {zh ? "本次执行" : "Attempt"} {durationText(attempt.duration)}
              </span>
            </header>
            {phases.map((phase) => (
              <div className="observe-phase" key={phase.name}>
                <div>
                  <strong>{phase.name}</strong>
                  <small>
                    {phase.count == null
                      ? zh
                        ? "最近记录"
                        : "Last recorded"
                      : zh
                        ? `${phase.count} 次`
                        : `${phase.count} calls`}
                  </small>
                </div>
                <div className="observe-phase-track">
                  <span
                    className={phase.outcome === "failed" ? "error" : ""}
                    style={{
                      width: `${Math.max(0.5, ((phase.durationMs || 0) / longest) * 100)}%`,
                    }}
                  />
                </div>
                <span className="observe-numeric">
                  {durationText(
                    phase.durationMs == null
                      ? undefined
                      : phase.durationMs / 1000,
                  )}
                </span>
                <Outcome value={phase.outcome} zh={zh} />
              </div>
            ))}
            {!phases.length && (
              <p className="observe-note">
                {zh
                  ? "本次执行没有阶段汇总记录。可查看已采集的调用 Span 和生命周期事件。"
                  : "No phase summaries for this attempt. Inspect collected spans and lifecycle events."}
              </p>
            )}
            {omitted > 0 && (
              <p className="observe-note">
                {zh
                  ? `另有 ${omitted} 次阶段记录已汇总省略。`
                  : `${omitted} phase records were omitted by aggregation.`}
              </p>
            )}
            <p className="observe-note">
              {zh
                ? "重复阶段展示累计工作时间，并行阶段的耗时可能重叠。"
                : "Repeated phases show accumulated work time; parallel phase durations may overlap."}
            </p>
          </section>
          <div className="observe-actions">
            <Button
              size="sm"
              variant="outline"
              onClick={() => onTrace(attempt.traceId, data?.until)}
            >
              {zh ? "调用瀑布图" : "Trace waterfall"}
              <ArrowUpRight data-icon="inline-end" />
            </Button>
            <Button
              size="sm"
              variant="outline"
              aria-expanded={showLogs}
              onClick={() => setShowLogs((v) => !v)}
            >
              {zh ? "原始错误与日志" : "Exception and logs"}
            </Button>
          </div>
          {showLogs && (
            <TraceLogs
              key={attempt.traceId}
              app={app}
              token={token}
              zh={zh}
              traceId={attempt.traceId}
              hours={Number(query.hours)}
              until={data?.until}
            />
          )}
          <details className="observe-processes">
            <summary>
              {zh ? "生命周期事件" : "Lifecycle events"} (
              {attempt.events.length})
            </summary>
            <ol className="observe-events">
              {attempt.events.map((event, i) => (
                <li key={i}>
                  <details>
                    <summary>
                      <time>{timeText(Number(event.timestamp), zh)}</time>
                      <strong>{event.event}</strong>
                      <span>{event.stage || event.outcome}</span>
                    </summary>
                    <AttributeTable values={{ ...event }} />
                  </details>
                </li>
              ))}
            </ol>
          </details>
        </>
      )}
      {!group && loaded.some((event) => event.event === "task_published") && (
        <details className="observe-processes">
          <summary>{zh ? "任务投递记录" : "Task publications"}</summary>
          <ol className="observe-events">
            {loaded
              .filter((event) => event.event === "task_published")
              .map((event, index) => (
                <li key={index}>
                  <details>
                    <summary>
                      <time>{timeText(Number(event.timestamp), zh)}</time>
                      <strong>{event.task_name || event.task_id}</strong>
                      <span>{event.outcome}</span>
                    </summary>
                    <AttributeTable values={{ ...event }} />
                    {event.trace_id && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => onTrace(event.trace_id!, data?.until)}
                      >
                        Trace <ArrowUpRight data-icon="inline-end" />
                      </Button>
                    )}
                  </details>
                </li>
              ))}
          </ol>
        </details>
      )}
      {!loading && data?.state === "ok" && !groups.length && (
        <ReadState
          zh={zh}
          empty={
            zh
              ? "当前筛选范围没有执行记录，可扩大时间范围或按任务编号查询。"
              : "No executions match these filters. Widen the time range or search by task ID."
          }
        />
      )}
      {data?.possibly_truncated && (
        <p className="observe-note mt-3">
          {zh
            ? "当前记录尚未完整读取，尝试次数和结果以已加载证据为准。"
            : "Records are incomplete; attempt counts and outcomes reflect loaded evidence."}
        </p>
      )}
      {data?.state === "ok" && data.next_offset != null && (
        <Button
          className="mt-3"
          size="sm"
          variant="outline"
          disabled={loading}
          onClick={() =>
            setPage((p) => ({
              ...p,
              offset: data.next_offset!,
              until: data.until,
            }))
          }
        >
          {zh ? "加载更早记录" : "Load earlier records"}
        </Button>
      )}
    </section>
  );
}
