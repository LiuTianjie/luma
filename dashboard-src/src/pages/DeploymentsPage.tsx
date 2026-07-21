import { useEffect, useMemo, useState } from "react";
import { RefreshCw, RotateCcw, Square } from "lucide-react";
import { cancelBuildRun, fetchBuildRun, fetchBuildRuns, fetchDeploymentEvent, fetchDeploymentHistory, retryBuildRunStream, type BuildRun, type DeploymentEvent } from "../deploy/deployApi";
import type { DeployStep } from "../deploy/types";
import { StepLog } from "../deploy/StepLog";
import { Badge, CodeCell, StatePill } from "../components/ui";
import { PageHeader } from "./PageHeader";
import { formatTimestamp } from "../format";
import { t } from "../i18n";
import type { Lang } from "../types";

// One timeline row, normalized from either a build run or a deploy event.
type TimelineItem = {
  id: string;
  origin: "build" | "cli" | "dashboard";
  title: string;
  subtitle: string;
  status: string;
  ts: number;
};

type DetailState = {
  item: TimelineItem;
  loading: boolean;
  steps: DeployStep[];
  error: string;
  retrying: boolean;
  canceling: boolean;
};

function isRetryableBuild(item: TimelineItem): boolean {
  // Only build runs can be retried (by their recorded parameters), and only when
  // they are not currently running.
  return item.origin === "build" && !["running", "succeeded", "active"].includes(item.status.toLowerCase());
}

function isCancelableBuild(item: TimelineItem): boolean {
  return item.origin === "build" && ["running", "canceling"].includes(item.status.toLowerCase());
}

const REFRESH_MS = 20000;

function originLabel(origin: TimelineItem["origin"], lang: Lang) {
  const zh = lang === "zh";
  if (origin === "build") return zh ? "构建部署" : "Build";
  if (origin === "dashboard") return zh ? "面板部署" : "Dashboard";
  return zh ? "CLI 部署" : "CLI";
}

// Map heterogeneous status strings to StatePill value buckets (good/warn/danger).
function statusValue(status: string) {
  const s = status.toLowerCase();
  if (["succeeded", "active", "ready", "running", "healthy"].includes(s)) return "running";
  if (["failed", "failed_partial"].includes(s)) return "failed";
  return "";
}

function buildToItem(run: BuildRun): TimelineItem {
  const repo = run.repository || run.source || run.id || "-";
  return {
    id: run.id || `build-${repo}`,
    origin: "build",
    title: repo,
    subtitle: [run.ref, run.buildNode].filter(Boolean).join(" · ") || (run.source || ""),
    status: run.status || "running",
    ts: run.updatedAt || run.createdAt || 0,
  };
}

function eventToItem(event: DeploymentEvent): TimelineItem {
  const origin = event.origin === "dashboard" ? "dashboard" : "cli";
  return {
    id: event.id || `${event.slug}-${event.createdAt}`,
    origin,
    title: event.name || event.slug || "-",
    subtitle: [event.kind === "compose" ? "compose" : "service", event.sourceName].filter(Boolean).join(" · "),
    status: event.status || "",
    ts: event.createdAt || 0,
  };
}

export function DeploymentsPage({ lang, token }: { lang: Lang; token: string }) {
  const zh = lang === "zh";
  const [builds, setBuilds] = useState<BuildRun[]>([]);
  const [events, setEvents] = useState<DeploymentEvent[]>([]);
  const [filter, setFilter] = useState<"all" | "build" | "dashboard" | "cli">("all");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<DetailState | null>(null);

  // Open the detail drawer for a row and lazily fetch its step log.
  const openDetail = async (item: TimelineItem) => {
    setDetail({ item, loading: true, steps: [], error: "", retrying: false, canceling: false });
    try {
      if (item.origin === "build") {
        const { run } = await fetchBuildRun(token, item.id);
        setDetail({ item: { ...item, status: run?.status || item.status }, loading: false, steps: (run?.events as DeployStep[]) || [], error: "", retrying: false, canceling: false });
      } else {
        const { event } = await fetchDeploymentEvent(token, item.id);
        setDetail({ item, loading: false, steps: event?.steps || [], error: "", retrying: false, canceling: false });
      }
    } catch (err) {
      setDetail({ item, loading: false, steps: [], error: err instanceof Error ? err.message : String(err), retrying: false, canceling: false });
    }
  };

  // Retry a failed build run by its recorded parameters, streaming live steps into
  // the open drawer. Refreshes the timeline when done.
  const retryBuild = async () => {
    setDetail((prev) => (prev ? { ...prev, retrying: true, error: "", steps: [{ name: "Build image", status: "progress", message: zh ? "重试已开始" : "Retry started" }] } : prev));
    const item = detail?.item;
    if (!item) return;
    try {
      await retryBuildRunStream(token, item.id, (step) =>
        setDetail((prev) => (prev && prev.item.id === item.id ? { ...prev, steps: [...prev.steps, step] } : prev)),
      );
      await load();
    } catch (err) {
      setDetail((prev) => (prev && prev.item.id === item.id ? { ...prev, error: err instanceof Error ? err.message : String(err) } : prev));
    } finally {
      setDetail((prev) => (prev && prev.item.id === item.id ? { ...prev, retrying: false } : prev));
    }
  };

  const cancelBuild = async () => {
    const item = detail?.item;
    if (!item) return;
    setDetail((prev) => (prev ? { ...prev, canceling: true, error: "" } : prev));
    try {
      const result = await cancelBuildRun(token, item.id);
      const status = result.run?.status || "canceling";
      setDetail((prev) => (
        prev && prev.item.id === item.id
          ? {
              ...prev,
              item: { ...prev.item, status },
              steps: (result.run?.events as DeployStep[]) || prev.steps,
            }
          : prev
      ));
      await load();
    } catch (err) {
      setDetail((prev) => (prev && prev.item.id === item.id ? { ...prev, error: err instanceof Error ? err.message : String(err) } : prev));
    } finally {
      setDetail((prev) => (prev && prev.item.id === item.id ? { ...prev, canceling: false } : prev));
    }
  };

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const [buildResult, eventResult] = await Promise.all([
        fetchBuildRuns(token).catch(() => ({ runs: [] as BuildRun[] })),
        fetchDeploymentHistory(token).catch(() => ({ events: [] as DeploymentEvent[] })),
      ]);
      setBuilds(buildResult.runs || []);
      setEvents(eventResult.events || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const items = useMemo(() => {
    const merged = [...builds.map(buildToItem), ...events.map(eventToItem)];
    merged.sort((a, b) => b.ts - a.ts);
    return merged;
  }, [builds, events]);

  const counts = useMemo(() => ({
    all: items.length,
    build: items.filter((i) => i.origin === "build").length,
    dashboard: items.filter((i) => i.origin === "dashboard").length,
    cli: items.filter((i) => i.origin === "cli").length,
  }), [items]);

  const visible = useMemo(
    () => (filter === "all" ? items : items.filter((i) => i.origin === filter)),
    [items, filter],
  );

  const filters: Array<{ key: typeof filter; label: string }> = [
    { key: "all", label: zh ? "全部" : "All" },
    { key: "build", label: zh ? "构建" : "Build" },
    { key: "dashboard", label: zh ? "面板" : "Dashboard" },
    { key: "cli", label: "CLI" },
  ];

  return (
    <>
      <PageHeader
        meta={{
          eyebrow: zh ? "部署记录" : "Deployments",
          title: zh ? "部署与构建时间线" : "Deployment and build timeline",
          description: zh
            ? "汇总仓库构建、CLI 和控制台发起的部署，按时间倒序，可按来源筛选。"
            : "Every repository build, CLI deploy, and dashboard deploy in one timeline, newest first, filterable by source.",
          metrics: [
            { label: zh ? "全部" : "Total", value: counts.all },
            { label: zh ? "构建" : "Build", value: counts.build },
            { label: "CLI", value: counts.cli },
            { label: zh ? "面板" : "Dashboard", value: counts.dashboard },
          ],
          action: (
            <button type="button" className="ghost" onClick={() => void load()} disabled={loading}>
              <RefreshCw size={16} aria-hidden="true" className={loading ? "spin" : undefined} />
              {zh ? "刷新" : "Refresh"}
            </button>
          ),
        }}
      />
      <article className="panel deployments-panel">
        <div className="credentials-tabs" role="tablist" aria-label={zh ? "来源筛选" : "Source filter"}>
          {filters.map((f) => (
            <button key={f.key} type="button" className={filter === f.key ? "active" : ""} onClick={() => setFilter(f.key)}>
              {f.label}
              <span className="deployments-filter-count">{f.key === "all" ? counts.all : counts[f.key]}</span>
            </button>
          ))}
        </div>
        {error ? <div className="alert alert-error"><span>{error}</span></div> : null}
        {visible.length ? (
          <ol className="deployments-timeline">
            {visible.map((item) => (
              <li
                className="deployments-row is-clickable"
                key={item.id}
                role="button"
                tabIndex={0}
                aria-label={`${t(lang, "details")}: ${item.title}`}
                onClick={() => void openDetail(item)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    void openDetail(item);
                  }
                }}
              >
                <span className="deployments-origin"><Badge value={originLabel(item.origin, lang)} /></span>
                <div className="deployments-main">
                  <CodeCell value={item.title} />
                  {item.subtitle ? <small>{item.subtitle}</small> : null}
                </div>
                <StatePill label={item.status || "-"} value={statusValue(item.status)} />
                <time className="deployments-time">{formatTimestamp(item.ts, lang)}</time>
              </li>
            ))}
          </ol>
        ) : (
          <div className="empty-inline">{zh ? "暂无部署记录" : "No deployments yet"}</div>
        )}
      </article>

      {detail ? (
        <div className="detail-backdrop" onClick={() => setDetail(null)}>
          <aside className="detail-drawer" onClick={(event) => event.stopPropagation()}>
            <header>
              <div>
                <p className="eyebrow">{originLabel(detail.item.origin, lang)}</p>
                <h2>{detail.item.title}</h2>
              </div>
              <button type="button" className="icon-button" onClick={() => setDetail(null)}>{t(lang, "close")}</button>
            </header>
            <dl>
              <div><dt>{zh ? "来源" : "Origin"}</dt><dd>{originLabel(detail.item.origin, lang)}</dd></div>
              <div><dt>{t(lang, "status")}</dt><dd>{detail.item.status || "-"}</dd></div>
              <div><dt>{zh ? "时间" : "Time"}</dt><dd>{formatTimestamp(detail.item.ts, lang)}</dd></div>
              {detail.item.subtitle ? <div><dt>{zh ? "详情" : "Detail"}</dt><dd>{detail.item.subtitle}</dd></div> : null}
            </dl>
            {isRetryableBuild(detail.item) ? (
              <button type="button" className="ghost" disabled={detail.retrying} onClick={() => void retryBuild()}>
                <RotateCcw size={15} aria-hidden="true" />
                {detail.retrying ? (zh ? "重试中…" : "Retrying…") : (zh ? "按原参数重试" : "Retry build")}
              </button>
            ) : null}
            {isCancelableBuild(detail.item) ? (
              <button type="button" className="ghost danger" disabled={detail.canceling || detail.item.status.toLowerCase() === "canceling"} onClick={() => void cancelBuild()}>
                <Square size={14} aria-hidden="true" />
                {detail.canceling || detail.item.status.toLowerCase() === "canceling"
                  ? (zh ? "正在取消…" : "Canceling…")
                  : (zh ? "取消构建" : "Cancel build")}
              </button>
            ) : null}
            <h3>{zh ? "步骤日志" : "Step log"}</h3>
            {detail.loading ? (
              <p className="deployment-config-empty">{zh ? "加载中…" : "Loading…"}</p>
            ) : detail.error ? (
              <div className="build-log-error">{detail.error}</div>
            ) : detail.steps.length ? (
              <StepLog steps={detail.steps} lang={lang} />
            ) : (
              <p className="deployment-config-empty">{zh ? "这条记录没有分步日志。" : "No step log recorded for this entry."}</p>
            )}
          </aside>
        </div>
      ) : null}
    </>
  );
}
