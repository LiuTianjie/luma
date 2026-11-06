import { overviewProgress, processMetrics, sourceReadable, type TaskMetric } from "./overviewModel";
import { durationText, timeText } from "./shared";
import type { MetricSource } from "./types";

export function OverviewProgress({ sources, tasks, sampledAt, zh, onTask }: {
  sources: Record<string, MetricSource>;
  tasks: TaskMetric[];
  sampledAt: number;
  zh: boolean;
  onTask: (name: string, queue: string) => void;
}) {
  const progress = overviewProgress(sources, tasks);
  const processes = processMetrics(sources, sampledAt);
  const fresh = processes.filter((p) => p.age != null && p.age < 120).length;
  const stale = processes.filter((p) => p.age != null && p.age >= 120).length;
  const count = (value?: number) => value == null ? "—" : value.toLocaleString(zh ? "zh-CN" : "en-GB", { maximumFractionDigits: 1 });
  const taskLink = (task: TaskMetric | undefined, source: string) => task ? (
    <button className="observe-progress-task" onClick={() => onTask(task.name, task.queue)}>
      {task.name}<small>{task.queue || (zh ? "查看执行记录" : "View executions")}</small>
    </button>
  ) : <span className="observe-stat-note">{!sourceReadable(sources[source])
    ? zh ? "指标暂不可用" : "Metric unavailable"
    : zh ? "暂无对应的活跃任务样本" : "No matching active-task samples"}</span>;
  const total = progress.counts && Object.values(progress.counts).reduce((sum, value) => sum + value, 0);
  return (
    <div className="observe-progress">
      <header className="observe-progress-heading">
        <div>
          <h3>{zh ? "执行进展" : "Execution progress"}</h3>
          <p>{zh ? "先看任务是否推进，再结合进程心跳与执行记录排查。" : "Read task progress alongside process heartbeats and execution records."}</p>
        </div>
        <span className="observe-note">{zh ? "采样于" : "Sampled"} {timeText(sampledAt, zh)}</span>
      </header>
      <div className="observe-progress-grid">
        <div className="observe-progress-stat">
          <span className="observe-stat-label">{zh ? "活跃任务" : "Active tasks"}</span>
          <strong>{count(progress.active)}</strong>
          <span className="observe-stat-note">{progress.active == null
            ? !sourceReadable(sources.active) ? zh ? "活跃指标暂不可用" : "Activity unavailable" : zh ? "暂无活跃数样本" : "No activity samples"
            : zh ? `${progress.activeTypes} 种任务 · 已观测执行数` : `${progress.activeTypes} task types · observed activity`}</span>
        </div>
        <div className="observe-progress-stat">
          <span className="observe-stat-label">{zh ? "最长执行" : "Longest execution"}</span>
          <strong>{durationText(progress.oldest?.oldest)}</strong>
          {taskLink(progress.oldest, "oldest")}
        </div>
        <div className="observe-progress-stat">
          <span className="observe-stat-label">{zh ? "最久未上报进展" : "Longest without reported progress"}</span>
          <strong>{durationText(progress.noProgress?.no_progress)}</strong>
          {taskLink(progress.noProgress, "no_progress")}
        </div>
        <div className="observe-progress-stat">
          <span className="observe-stat-label">{zh ? "最近完成" : "Last completion"}</span>
          <strong>{progress.last != null
            ? `${durationText(Math.max(0, sampledAt - progress.last))}${zh ? " 前" : " ago"}` : "—"}</strong>
          <span className="observe-stat-note">{progress.last != null ? timeText(progress.last, zh)
            : !sourceReadable(sources.last_finished) ? zh ? "完成时间暂不可用" : "Completion time unavailable"
              : zh ? "近 72 小时暂无完成时间样本" : "No completion-time samples in the last 72 hours"}</span>
        </div>
      </div>
      <div className="observe-completion-summary">
        <div>
          <span>{zh ? "所选时段完成记录" : "Completions in selected range"}</span>
          <strong>{count(total)}</strong>
        </div>
        {progress.counts && total === 0 ? (
          <p>{zh ? "查询成功，该时段未找到完成记录。" : "The query succeeded; no completions were recorded in this range."}</p>
        ) : progress.counts ? (
          <div className="observe-result-counts">
            {([['succeeded', '成功', 'Succeeded'], ['failed', '失败', 'Failed'], ['retrying', '重试', 'Retrying'], ['cancelled', '取消', 'Cancelled'], ['unknown', '未知', 'Unknown']] as const).map(([key, cn, en]) => (
              <span key={key} className={key === "failed" && progress.counts![key] > 0 ? "observe-error-text" : undefined}>
                {zh ? cn : en} <b>{count(progress.counts![key])}</b>
              </span>
            ))}
          </div>
        ) : <p>{zh ? "完成记录暂不可用，不能按 0 次判断。" : "Completion records are unavailable, not a measured zero."}</p>}
      </div>
      <div className="observe-heartbeat-summary">
        <span>{zh ? "进程心跳" : "Process heartbeats"} · {sources.processes?.state === "ok"
          ? zh ? `${fresh} 个新鲜${stale ? ` / ${stale} 个已过期` : ""}` : `${fresh} fresh${stale ? ` / ${stale} stale` : ""}`
          : "—"}</span>
        <span>{zh ? "心跳新鲜不代表任务有进展；进展依赖任务主动上报。" : "Fresh heartbeats do not prove task progress; progress requires task instrumentation."}</span>
      </div>
    </div>
  );
}
