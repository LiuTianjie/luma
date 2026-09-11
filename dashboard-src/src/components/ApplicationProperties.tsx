import type { ReactNode } from "react";

/** Label/value pairs must keep complete identifiers readable, including unbroken digests. */
export function ApplicationProperties({ items }: { items: Array<{ label: string; value: ReactNode }> }) {
  return (
    <dl className="grid grid-cols-1 gap-x-8 gap-y-4 sm:grid-cols-2">
      {items.map(({ label, value }) => (
        <div key={label} className="flex min-w-0 flex-col gap-1">
          <dt className="text-xs text-muted-foreground">{label}</dt>
          <dd className="m-0 min-w-0 text-sm font-medium wrap-break-word">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function ApplicationVersionEntry({ version, current, image, imageLabel, submitted, submittedLabel, stable, action }: {
  version: string; current: boolean; image: string; imageLabel: string; submitted: string; submittedLabel: string; stable?: ReactNode; action: ReactNode;
}) {
  return (
    <article className="flex flex-col gap-3 border-b py-4 last:border-b-0">
      <header className="flex flex-wrap items-center gap-2">
        <strong className="text-sm font-medium">{version}</strong>
        {stable}
        <div className="ml-auto">{action}</div>
      </header>
      <ApplicationProperties items={[
        { label: imageLabel, value: <code className="font-mono text-xs break-all">{image}</code> },
        { label: submittedLabel, value: submitted },
      ]} />
    </article>
  );
}
