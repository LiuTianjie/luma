import type { ReactNode } from "react";

export function PrimaryCell({ title, meta }: { title: string; meta?: string }) {
  return (
    <span className="primary-cell">
      <strong>{title || "-"}</strong>
      {meta && meta !== title ? <small>{meta}</small> : null}
    </span>
  );
}

export function Badge({ value }: { value: string }) {
  return <span className="badge">{value}</span>;
}

export function BadgeGroup({ children }: { children: ReactNode }) {
  return <span className="badge-group">{children}</span>;
}

export function CodeCell({ value }: { value: string }) {
  return <code>{value}</code>;
}

export function StatePill({ label, value }: { label: string; value?: string }) {
  const kind = ["ready", "running", "healthy"].includes(value || "")
    ? "good"
    : ["failed", "missing", "bad"].includes(value || "")
      ? "danger"
      : "warn";
  return <span className={`badge ${kind}`}>{label}</span>;
}
