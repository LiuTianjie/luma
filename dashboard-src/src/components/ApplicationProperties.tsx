import type { ReactNode } from "react";

/** Label/value pairs must keep complete identifiers readable, including unbroken digests. */
export function ApplicationProperties({ items }: { items: Array<{ label: string; value: ReactNode }> }) {
  return <dl className="application-properties">{items.map(({ label, value }) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
}

export function ApplicationVersionEntry({ version, current, image, imageLabel, submitted, submittedLabel, stable, action }: {
  version: string; current: boolean; image: string; imageLabel: string; submitted: string; submittedLabel: string; stable?: ReactNode; action: ReactNode;
}) {
  return <article className={`application-version-entry${current ? " is-current" : ""}`}>
    <header><strong>{version}</strong>{stable}<div className="application-version-action">{action}</div></header>
    <ApplicationProperties items={[
      { label: imageLabel, value: <code className="application-full-value">{image}</code> },
      { label: submittedLabel, value: submitted },
    ]} />
  </article>;
}
