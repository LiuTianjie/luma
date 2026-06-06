import { useEffect, useMemo, useState } from "react";
import { fetchMetricsHistory } from "../metricsApi";
import type { DashboardIssue, Lang, MetricPoint } from "../types";
import { Badge, StatePill } from "./ui";
import { Sparkline } from "./charts";

function issuePillValue(severity?: string) {
  if (severity === "critical") return "failed";
  if (severity === "warning") return "pending";
  return "ready";
}

// Trend-based alert kinds carry a metric series worth visualizing inline.
const TREND_KINDS: Record<string, { kind: "node" | "service"; series: string }> = {
  "node-cpu": { kind: "node", series: "cpuPercent" },
  "node-memory": { kind: "node", series: "memoryUsedPercent" },
};

const TREND_WINDOW_SECONDS = 900;

function trendKey(kind: string, target: string) {
  return `${kind}:${target}`;
}

export function IssuesPanel({ lang, issues, token }: { lang: Lang; issues: DashboardIssue[]; token?: string }) {
  const critical = issues.filter((issue) => issue.severity === "critical").length;
  const warning = issues.filter((issue) => issue.severity === "warning").length;
  const info = issues.filter(
    (issue) => issue.severity && !["critical", "warning"].includes(issue.severity),
  ).length;

  const trendTargets = useMemo(() => {
    const seen = new Set<string>();
    const targets: { kind: "node" | "service"; series: string; target: string }[] = [];
    for (const issue of issues) {
      const spec = issue.kind ? TREND_KINDS[issue.kind] : undefined;
      if (!spec || !issue.target) continue;
      const key = trendKey(spec.kind, issue.target);
      if (seen.has(key)) continue;
      seen.add(key);
      targets.push({ ...spec, target: issue.target });
    }
    return targets;
  }, [issues]);

  const [trends, setTrends] = useState<Record<string, MetricPoint[]>>({});
  const signature = trendTargets.map((item) => `${item.kind}:${item.target}:${item.series}`).sort().join(",");

  useEffect(() => {
    if (!token || !trendTargets.length) {
      setTrends({});
      return;
    }
    let cancelled = false;
    const controller = new AbortController();
    (async () => {
      const entries = await Promise.all(
        trendTargets.map(async (item) => {
          try {
            const payload = await fetchMetricsHistory({
              token,
              kind: item.kind,
              name: item.target,
              window: TREND_WINDOW_SECONDS,
              signal: controller.signal,
            });
            return [trendKey(item.kind, item.target), payload.series?.[item.series] || []] as const;
          } catch {
            return [trendKey(item.kind, item.target), [] as MetricPoint[]] as const;
          }
        }),
      );
      if (!cancelled) setTrends(Object.fromEntries(entries));
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, signature]);

  return (
    <article className="panel issues-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">{lang === "zh" ? "异常" : "Issues"}</p>
          <h2>{lang === "zh" ? "需要关注" : "Needs Attention"}</h2>
        </div>
        <span>{issues.length}</span>
      </div>
      {issues.length ? (
        <div className="issues-list">
          {issues.map((issue, index) => {
            const spec = issue.kind ? TREND_KINDS[issue.kind] : undefined;
            const points = spec && issue.target ? trends[trendKey(spec.kind, issue.target)] : undefined;
            return (
              <div className="issue-row" key={`${issue.kind || "issue"}-${issue.target || index}-${index}`}>
                <StatePill
                  label={issue.severity || "info"}
                  value={issuePillValue(issue.severity)}
                />
                <span>
                  <strong>{issue.message || "-"}</strong>
                  <small>{[issue.kind, issue.target].filter(Boolean).join(" / ") || "-"}</small>
                </span>
                {points && points.length > 1 ? (
                  <Sparkline points={points} range={{ min: 0, max: 100 }} color="var(--amber)" width={88} height={28} />
                ) : null}
              </div>
            );
          })}
        </div>
      ) : (
        <div className="issues-empty">
          <Badge value={lang === "zh" ? "当前没有明显异常" : "No active issues"} />
        </div>
      )}
      <div className="issues-summary">
        <Badge value={`critical ${critical}`} />
        <Badge value={`warning ${warning}`} />
        {info ? <Badge value={`info ${info}`} /> : null}
      </div>
    </article>
  );
}
