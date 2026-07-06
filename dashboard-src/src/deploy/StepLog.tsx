import type { DeployStep } from "./types";
import type { Lang } from "../types";

// Shared renderer for a deploy/build step log (clone → build → push → deploy, or a
// native deploy's steps). Steps carry a status that colors each row (ok/fail/…).
// Consolidates the markup that previously lived in GithubImportPanel and DeploymentsPage.
export function StepLog({
  steps,
  lang,
  variant = "compact",
  waitingLabel,
  keyPrefix = "",
}: {
  steps: DeployStep[];
  lang?: Lang;
  // "compact" renders messages as " - message"; "plain" renders the raw message.
  variant?: "compact" | "plain";
  // When set, an empty step list renders this single placeholder row instead of nothing.
  waitingLabel?: string;
  keyPrefix?: string;
}) {
  const named = steps.filter((step) => step.name);
  const zh = (lang ?? "zh") === "zh";
  const placeholder = waitingLabel ?? (zh ? "等待日志事件" : "Waiting for events");
  return (
    <ol className="deploy-step-log build-step-log">
      {named.map((step, index) => (
        <li key={`${keyPrefix}${step.name}-${index}`} className={`step-${step.status || "ok"}`}>
          <strong>{step.name}</strong>
          {step.message ? <span>{variant === "compact" ? ` - ${step.message}` : step.message}</span> : null}
        </li>
      ))}
      {waitingLabel !== undefined && !named.length ? (
        <li className="step-start"><strong>{placeholder}</strong></li>
      ) : null}
    </ol>
  );
}
