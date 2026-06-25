import { CheckCircle2, CircleDot, Clock3, GitBranch, Loader2, XCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Badge, CodeCell, StatePill } from "../components/ui";
import type { DashboardOperation, Lang, OperationStep } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";

type StageDefinition = {
  key: string;
  zh: string;
  en: string;
  match: string[];
};

type TimelineStep = OperationStep & {
  startedTime?: number;
};

const STANDARD_STAGES: StageDefinition[] = [
  { key: "parse", zh: "Parse manifest", en: "Parse manifest", match: ["parse manifest", "parse compose"] },
  { key: "node", zh: "Resolve node pin", en: "Resolve node pin", match: ["resolve node pin"] },
  { key: "image", zh: "Resolve image", en: "Resolve image", match: ["resolve image", "build image"] },
  { key: "storage", zh: "Prepare storage", en: "Prepare storage", match: ["prepare managed storage"] },
  { key: "render", zh: "Render job", en: "Render job", match: ["render nomad", "render compose", "render blue-green", "render initial"] },
  { key: "dns", zh: "Sync DNS", en: "Sync DNS", match: ["sync dns"] },
  { key: "deploy", zh: "Deploy Nomad", en: "Deploy Nomad", match: ["deploy nomad", "deploy compose", "deploy blue-green", "deploy initial"] },
  { key: "health", zh: "Wait health", en: "Wait health", match: ["wait for revision health", "wait for initial health", "wait health"] },
  { key: "route", zh: "Switch route", en: "Switch route", match: ["switch route", "activate initial route", "write route", "resolve relay"] },
  { key: "probe", zh: "Probe", en: "Probe", match: ["probe public route"] },
  { key: "retire", zh: "Retire old revisions", en: "Retire old revisions", match: ["retire old revisions"] },
];

const BLUE_GREEN_STAGES: StageDefinition[] = [
  { key: "candidate", zh: "Candidate revision", en: "Candidate revision", match: ["render blue-green", "write revision", "deploy blue-green"] },
  { key: "active", zh: "Active revision", en: "Active revision", match: ["active revision"] },
  { key: "health", zh: "Candidate health", en: "Candidate health", match: ["wait for revision health"] },
  { key: "cutover", zh: "Route cutover", en: "Route cutover", match: ["switch route"] },
  { key: "probe", zh: "Probe", en: "Probe", match: ["probe public route"] },
  { key: "retire", zh: "Retired revisions", en: "Retired revisions", match: ["retire old revisions"] },
];

function operationTitle(operation: DashboardOperation) {
  return operation.target?.name || operation.target?.slug || operation.target?.repoUrl || operation.target?.sourceName || operation.id || "-";
}

function statusLabel(lang: Lang, status?: string) {
  const value = (status || "running").toLowerCase();
  if (lang === "zh") {
    if (value === "succeeded") return "成功";
    if (value === "failed") return "失败";
    if (value === "running") return "进行中";
    return value || "未知";
  }
  if (value === "succeeded") return "Succeeded";
  if (value === "failed") return "Failed";
  if (value === "running") return "Running";
  return value || "Unknown";
}

function sourceLabel(lang: Lang, source?: string) {
  const value = (source || "api").toLowerCase();
  if (lang === "zh") {
    if (value === "cli") return "CLI";
    if (value === "dashboard") return "Dashboard";
    if (value === "github-import") return "GitHub import";
    return "API";
  }
  if (value === "cli") return "CLI";
  if (value === "dashboard") return "Dashboard";
  if (value === "github-import") return "GitHub import";
  return "API";
}

function kindLabel(lang: Lang, kind?: string) {
  const value = (kind || "").toLowerCase();
  if (value === "compose-deploy") return lang === "zh" ? "Compose 部署" : "Compose deploy";
  if (value === "github-import") return lang === "zh" ? "GitHub 导入" : "GitHub import";
  return lang === "zh" ? "服务部署" : "Service deploy";
}

function timeLabel(value?: number) {
  if (!value) return "-";
  return new Date(value * 1000).toLocaleString();
}

function durationLabel(operation: DashboardOperation, lang: Lang) {
  const started = Number(operation.startedAt || 0);
  if (!started) return "-";
  const end = Number(operation.finishedAt || operation.updatedAt || Math.floor(Date.now() / 1000));
  const seconds = Math.max(0, end - started);
  if (seconds < 60) return lang === "zh" ? `${seconds}s` : `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}m ${rest}s`;
}

function dedupeOperations(operations: DashboardOperation[]) {
  const seen = new Set<string>();
  return operations.filter((operation) => {
    const id = operation.id || `${operation.kind}-${operation.startedAt}-${operationTitle(operation)}`;
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

function stepStatusClass(status?: string) {
  const value = (status || "").toLowerCase();
  if (value === "ok" || value === "done" || value === "succeeded") return "done";
  if (value === "fail" || value === "failed") return "failed";
  if (value === "start" || value === "running") return "running";
  return "pending";
}

function isBlueGreen(operation: DashboardOperation) {
  const rolloutMode = String(operation.result?.rolloutMode || "").toLowerCase();
  if (rolloutMode === "initial") return false;
  if (rolloutMode === "blue-green") return true;
  return (operation.steps || []).some((step) => /blue-green|switch route to revision|retire old revisions|retire previous compose revision/i.test(step.name || ""));
}

function stageState(stage: StageDefinition, steps: OperationStep[], operation: DashboardOperation) {
  if (stage.key === "active" && operation.result?.activeRevision) return "done";
  const matched = steps.filter((step) => {
    const name = (step.name || "").toLowerCase();
    return stage.match.some((pattern) => name.includes(pattern));
  });
  if (!matched.length) return "pending";
  if (matched.some((step) => stepStatusClass(step.status) === "failed")) return "failed";
  return stepStatusClass(matched[matched.length - 1]?.status);
}

function stageLabel(stage: StageDefinition, lang: Lang) {
  return lang === "zh" ? stage.zh : stage.en;
}

function compactTimelineSteps(steps: OperationStep[]): TimelineStep[] {
  const rows: TimelineStep[] = [];
  const activeByName = new Map<string, TimelineStep>();

  steps.forEach((step) => {
    const name = step.name || "Operation";
    const status = stepStatusClass(step.status);
    if (status === "running") {
      const row: TimelineStep = { ...step, startedTime: step.time };
      rows.push(row);
      activeByName.set(name, row);
      return;
    }

    const active = activeByName.get(name);
    if (active) {
      active.status = step.status;
      active.message = step.message || active.message;
      active.time = step.time || active.time;
      active.requestId = step.requestId || active.requestId;
      active.code = step.code || active.code;
      if (step.detail !== undefined) active.detail = step.detail;
      activeByName.delete(name);
      return;
    }

    rows.push({ ...step });
  });

  return rows;
}

function StepIcon({ status }: { status?: string }) {
  const kind = stepStatusClass(status);
  if (kind === "done") return <CheckCircle2 size={16} aria-hidden="true" />;
  if (kind === "failed") return <XCircle size={16} aria-hidden="true" />;
  if (kind === "running") return <Loader2 size={16} aria-hidden="true" />;
  return <CircleDot size={16} aria-hidden="true" />;
}

export function DeploymentsPage({ lang, vm }: { lang: Lang; vm: DashboardViewModel }) {
  const zh = lang === "zh";
  const operations = useMemo(() => dedupeOperations([...vm.operationsRunning, ...vm.operationsRecent]), [vm.operationsRecent, vm.operationsRunning]);
  const [selectedId, setSelectedId] = useState("");

  useEffect(() => {
    if (!operations.length) {
      setSelectedId("");
      return;
    }
    if (!selectedId || !operations.some((operation) => operation.id === selectedId)) {
      setSelectedId(operations[0].id || "");
    }
  }, [operations, selectedId]);

  const selected = operations.find((operation) => operation.id === selectedId) || operations[0];
  const failedRecent = vm.operationsFailed.slice(0, 3);
  const stages = selected && isBlueGreen(selected) ? BLUE_GREEN_STAGES : STANDARD_STAGES;
  const timelineSteps = useMemo(() => compactTimelineSteps(selected?.steps || []), [selected]);

  return (
    <>
      <section className="page-toolbar deployments-toolbar" aria-labelledby="deployments-title">
        <div className="page-toolbar-copy">
          <p className="eyebrow">{zh ? "部署控制面" : "Deployment control plane"}</p>
          <h1 id="deployments-title">{zh ? "全局部署流水" : "Global deployment flow"}</h1>
          <p>{zh ? "CLI、Dashboard、GitHub import 与 API 触发的部署都会在这里汇合。" : "CLI, Dashboard, GitHub import, and API-triggered deployments converge here."}</p>
        </div>
        <div className="page-toolbar-metrics deployments-metrics">
          <span><strong>{vm.operationsRunning.length}</strong><small>{zh ? "正在部署" : "Running"}</small></span>
          <span><strong>{vm.operationsFailed.length}</strong><small>{zh ? "最近失败" : "Recent failed"}</small></span>
          <span><strong>{operations.length}</strong><small>{zh ? "可见流水" : "Visible flows"}</small></span>
          <span><strong>{selected ? durationLabel(selected, lang) : "-"}</strong><small>{zh ? "当前耗时" : "Selected duration"}</small></span>
        </div>
      </section>

      <section className="deployments-grid" aria-label={zh ? "部署流水" : "Deployment operations"}>
        <article className="panel deployments-list-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">{zh ? "流水列表" : "Operations"}</p>
              <h2>{zh ? "最近和进行中" : "Recent and running"}</h2>
            </div>
            <Badge value={`${operations.length}`} />
          </div>
          <div className="operation-list">
            {operations.length ? operations.map((operation) => {
              const status = (operation.status || "running").toLowerCase();
              const active = operation.id === selected?.id;
              return (
                <button
                  type="button"
                  className={active ? "operation-row active" : "operation-row"}
                  key={operation.id || `${operation.kind}-${operation.startedAt}`}
                  onClick={() => setSelectedId(operation.id || "")}
                >
                  <span className={`operation-status-dot ${status}`} aria-hidden="true" />
                  <span className="operation-row-main">
                    <strong>{operationTitle(operation)}</strong>
                    <small>{kindLabel(lang, operation.kind)} · {sourceLabel(lang, operation.source)} · {operation.phase || "-"}</small>
                  </span>
                  <span className="operation-row-meta">
                    <StatePill label={statusLabel(lang, operation.status)} value={status} />
                    <small>{durationLabel(operation, lang)}</small>
                  </span>
                </button>
              );
            }) : (
              <div className="empty-inline">{zh ? "暂无部署流水" : "No deployment operations yet"}</div>
            )}
          </div>
        </article>

        <article className="panel operation-detail-panel">
          {selected ? (
            <>
              <div className="operation-detail-heading">
                <div>
                  <p className="eyebrow">{selected.id}</p>
                  <h2>{operationTitle(selected)}</h2>
                  <small>{kindLabel(lang, selected.kind)} · {sourceLabel(lang, selected.source)} · {selected.actor || "-"}</small>
                </div>
                <StatePill label={statusLabel(lang, selected.status)} value={selected.status} />
              </div>

              <div className="operation-facts">
                <span><small>{zh ? "开始" : "Started"}</small><strong>{timeLabel(selected.startedAt)}</strong></span>
                <span><small>{zh ? "更新" : "Updated"}</small><strong>{timeLabel(selected.updatedAt)}</strong></span>
                <span><small>{zh ? "耗时" : "Duration"}</small><strong>{durationLabel(selected, lang)}</strong></span>
                <span><small>{zh ? "目标" : "Target"}</small><strong>{selected.target?.domain || selected.target?.region || selected.target?.buildNode || "-"}</strong></span>
              </div>

              <div className="operation-stage-strip" aria-label={zh ? "阶段流程" : "Stage flow"}>
                {stages.map((stage) => {
                  const state = stageState(stage, selected.steps || [], selected);
                  return (
                    <span className={`operation-stage ${state}`} key={stage.key}>
                      <i aria-hidden="true" />
                      <b>{stageLabel(stage, lang)}</b>
                    </span>
                  );
                })}
              </div>

              {selected.error ? (
                <div className="operation-error">
                  <XCircle size={17} aria-hidden="true" />
                  <span>{selected.error}</span>
                </div>
              ) : null}

              {isBlueGreen(selected) ? (
                <div className="blue-green-strip">
                  <GitBranch size={17} aria-hidden="true" />
                  <span>
                    <b>{zh ? "蓝绿发布" : "Blue-green"}</b>
                    <small>
                      {[
                        selected.result?.activeRevision ? `active ${String(selected.result.activeRevision)}` : "",
                        Array.isArray(selected.result?.retiredRevisions) ? `retired ${(selected.result.retiredRevisions as unknown[]).length}` : "",
                      ].filter(Boolean).join(" · ") || (zh ? "等待 candidate revision 切流" : "Waiting for candidate revision cutover")}
                    </small>
                  </span>
                </div>
              ) : null}

              <div className="operation-timeline">
                {timelineSteps.length ? timelineSteps.map((step, index) => (
                  <div className={`operation-timeline-row ${stepStatusClass(step.status)}`} key={`${step.name || "step"}-${index}`}>
                    <span className="operation-timeline-icon"><StepIcon status={step.status} /></span>
                    <div>
                      <strong>{step.name || "-"}</strong>
                      <small>{step.message || "-"}</small>
                    </div>
                    <time>{step.time ? timeLabel(step.time) : ""}</time>
                  </div>
                )) : (
                  <div className="empty-inline">{zh ? "尚未写入步骤" : "No steps recorded yet"}</div>
                )}
              </div>
            </>
          ) : (
            <div className="empty-inline">{zh ? "选择一条部署流水查看详情" : "Select an operation to inspect the flow"}</div>
          )}
        </article>

        <aside className="panel deployments-failure-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">{zh ? "失败优先" : "Failure queue"}</p>
              <h2>{zh ? "最近失败部署" : "Recent failures"}</h2>
            </div>
            <Clock3 size={17} aria-hidden="true" />
          </div>
          <div className="failure-mini-list">
            {failedRecent.length ? failedRecent.map((operation) => (
              <button type="button" key={operation.id} onClick={() => setSelectedId(operation.id || "")}>
                <XCircle size={16} aria-hidden="true" />
                <span>
                  <strong>{operationTitle(operation)}</strong>
                  <small>{operation.error || operation.phase || "-"}</small>
                </span>
              </button>
            )) : (
              <div className="empty-inline">{zh ? "暂无失败部署" : "No recent failed deployments"}</div>
            )}
          </div>
          <div className="operation-id-box">
            <small>{zh ? "当前 Operation" : "Current operation"}</small>
            <CodeCell value={selected?.id || "-"} />
          </div>
        </aside>
      </section>
    </>
  );
}
