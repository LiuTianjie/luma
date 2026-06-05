import type { DashboardIssue, Lang } from "../types";
import { Badge, StatePill } from "./ui";

function issuePillValue(severity?: string) {
  if (severity === "critical") return "failed";
  if (severity === "warning") return "pending";
  return "ready";
}

export function IssuesPanel({ lang, issues }: { lang: Lang; issues: DashboardIssue[] }) {
  const critical = issues.filter((issue) => issue.severity === "critical").length;
  const warning = issues.filter((issue) => issue.severity === "warning").length;
  const info = issues.filter(
    (issue) => issue.severity && !["critical", "warning"].includes(issue.severity),
  ).length;
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
          {issues.map((issue, index) => (
            <div className="issue-row" key={`${issue.kind || "issue"}-${issue.target || index}-${index}`}>
              <StatePill
                label={issue.severity || "info"}
                value={issuePillValue(issue.severity)}
              />
              <span>
                <strong>{issue.message || "-"}</strong>
                <small>{[issue.kind, issue.target].filter(Boolean).join(" / ") || "-"}</small>
              </span>
            </div>
          ))}
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
