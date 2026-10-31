import type { AriaAttributes, ReactNode } from "react";
import { Badge as UiBadge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";

export type SelectOption = {
  value: string;
  label: ReactNode;
  disabled?: boolean;
};

export function SelectControl({
  value,
  options,
  onChange,
  placeholder = "-",
  disabled = false,
  ariaLabel,
  className = "",
  id,
  ...accessibility
}: {
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  placeholder?: ReactNode;
  disabled?: boolean;
  ariaLabel?: string;
  className?: string;
  id?: string;
  "aria-describedby"?: string;
  "aria-labelledby"?: string;
  "aria-invalid"?: AriaAttributes["aria-invalid"];
}) {
  const emptyValue = "__empty__";
  const encode = (item: string) => (item === "" ? emptyValue : item);
  const items = options.map((option) => ({
    value: encode(option.value),
    label: typeof option.label === "string" ? option.label : option.value || String(placeholder),
    disabled: option.disabled,
  }));

  return (
    <Select
      value={encode(value)}
      onValueChange={(next) => {
        if (typeof next === "string") onChange(next === emptyValue ? "" : next);
      }}
      disabled={disabled}
      items={items}
    >
      <SelectTrigger id={id} aria-label={ariaLabel} {...accessibility} className={cn("w-full min-w-0", className)}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false} side="bottom">
        <SelectGroup>
          {options.map((option) => (
            <SelectItem key={encode(option.value)} value={encode(option.value)} disabled={option.disabled}>
              {option.label}
            </SelectItem>
          ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  );
}

export function PrimaryCell({ title, meta }: { title: string; meta?: string }) {
  return (
    <span className="flex min-w-0 flex-col gap-0.5">
      <strong title={title} className="truncate text-sm font-medium">{title || "-"}</strong>
      {meta && meta !== title ? <small title={meta} className="truncate text-xs text-muted-foreground">{meta}</small> : null}
    </span>
  );
}

export function Badge({ value }: { value: string }) {
  return <UiBadge variant="secondary">{value}</UiBadge>;
}

export function BadgeGroup({ children }: { children: ReactNode }) {
  return <span className="flex flex-wrap items-center gap-1">{children}</span>;
}

/**
 * A compact KPI tile: small label (optionally with an icon or action), a large
 * value with optional context, and an optional footer line or meter. Tone colours
 * the icon and value only when something needs a look.
 */
export function StatTile({ label, icon: Icon, value, detail, footer, tone = "normal", action, className }: {
  label: ReactNode;
  icon?: (props: { className?: string; "aria-hidden"?: boolean }) => ReactNode;
  value: ReactNode;
  detail?: ReactNode;
  footer?: ReactNode;
  tone?: "normal" | "warning" | "critical";
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-2 rounded-lg bg-card px-4 py-3.5 shadow-xs ring-1 ring-border", className)}>
      <span className="flex min-w-0 items-center gap-2 text-xs font-medium text-muted-foreground">
        {Icon ? <Icon className={cn("size-3.5 shrink-0", tone === "critical" ? "text-destructive" : tone === "warning" ? "text-warning" : "")} aria-hidden={true} /> : null}
        <span className="truncate">{label}</span>
        {action ? <span className="ml-auto shrink-0">{action}</span> : null}
      </span>
      <span className="flex min-w-0 items-baseline gap-1.5">
        <strong className={cn("text-xl/none font-semibold tracking-[-0.01em] tabular-nums", tone === "critical" && "text-destructive-foreground", tone === "warning" && "text-warning-foreground")}>{value}</strong>
        {detail ? <span className="truncate text-xs text-muted-foreground">{detail}</span> : null}
      </span>
      {footer ? <div className="min-w-0 text-xs text-muted-foreground">{footer}</div> : null}
    </div>
  );
}

/** Usage tone shared by meters: neutral below 75%, amber to 90%, red above. */
export function usageTone(value?: number): "normal" | "warning" | "critical" {
  if (typeof value !== "number" || !Number.isFinite(value)) return "normal";
  return value >= 90 ? "critical" : value >= 75 ? "warning" : "normal";
}

/**
 * A compact inline meter (Kumo Meter) for CPU, memory or disk. The bar takes a
 * tone colour only when usage is high, so normal load reads as calm grey-blue.
 */
export function UsageMeter({ value, label, className }: { value?: number; label?: string; className?: string }) {
  const known = typeof value === "number" && Number.isFinite(value);
  const clamped = known ? Math.max(0, Math.min(100, value)) : 0;
  const tone = usageTone(value);
  return (
    <span className={cn("inline-flex min-w-0 items-center gap-2", className)}>
      <span
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={known ? Math.round(clamped) : undefined}
        aria-valuetext={known ? `${Math.round(clamped)}%` : "—"}
        className="relative h-1.5 w-full max-w-24 min-w-10 flex-1 overflow-hidden rounded-full bg-secondary"
      >
        <span
          className={cn("absolute inset-y-0 left-0 rounded-full transition-[width] duration-300", tone === "critical" ? "bg-destructive" : tone === "warning" ? "bg-warning" : "bg-primary/80")}
          style={{ width: `${clamped}%` }}
        />
      </span>
      <span className={cn("w-8 shrink-0 text-right text-xs tabular-nums", known ? "text-foreground" : "text-subtle-foreground", tone === "critical" && "font-medium text-destructive-foreground", tone === "warning" && "font-medium text-warning-foreground")}>
        {known ? `${Math.round(clamped)}%` : "—"}
      </span>
    </span>
  );
}

export function CodeCell({ value }: { value: string }) {
  return <code className="block max-w-full font-mono text-xs whitespace-normal break-all">{value}</code>;
}

const SUCCESS_STATES = ["ready", "running", "healthy", "active", "succeeded", "success", "stable", "available", "enabled", "sent", "configured", "resolved"];
const DANGER_STATES = ["failed", "missing", "bad", "down", "error", "failed_partial", "critical", "firing"];
const WARNING_STATES = ["pending", "degraded", "drain", "draining", "warning", "starting", "deploying", "queued", "retrying"];

export function stateVariant(value?: string): "success" | "destructive" | "warning" | "outline" {
  const normalized = (value || "").toLowerCase();
  if (SUCCESS_STATES.includes(normalized)) return "success";
  if (DANGER_STATES.includes(normalized)) return "destructive";
  if (WARNING_STATES.includes(normalized)) return "warning";
  return "outline";
}

/**
 * Status badge coloured by meaning. Healthy and unknown states use the quiet dot
 * form so lists of running things stay calm; warnings and failures are filled so
 * they stand out. A missing label renders nothing.
 */
export function StatePill({ label, value }: { label: string; value?: string }) {
  if (!label || label === "-" || label === "—") return null;
  const variant = stateVariant(value);
  return <UiBadge variant={variant} appearance={variant === "success" || variant === "outline" ? "dot" : "filled"}>{label}</UiBadge>;
}
