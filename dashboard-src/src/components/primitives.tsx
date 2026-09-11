import type { ReactNode } from "react";
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
}: {
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  placeholder?: ReactNode;
  disabled?: boolean;
  ariaLabel?: string;
  className?: string;
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
      <SelectTrigger aria-label={ariaLabel} className={cn("w-full min-w-40", className)}>
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
      <strong className="truncate text-sm font-medium">{title || "-"}</strong>
      {meta && meta !== title ? <small className="truncate text-xs text-muted-foreground">{meta}</small> : null}
    </span>
  );
}

export function Badge({ value }: { value: string }) {
  return <UiBadge variant="secondary">{value}</UiBadge>;
}

export function BadgeGroup({ children }: { children: ReactNode }) {
  return <span className="flex flex-wrap items-center gap-1">{children}</span>;
}

export function CodeCell({ value }: { value: string }) {
  return <code className="font-mono text-xs break-all">{value}</code>;
}

export function StatePill({ label, value }: { label: string; value?: string }) {
  const normalized = (value || "").toLowerCase();
  const variant = ["ready", "running", "healthy", "active", "succeeded", "stable", "available"].includes(normalized)
    ? "success"
    : ["failed", "missing", "bad", "down", "error", "failed_partial", "critical"].includes(normalized)
      ? "destructive"
      : ["pending", "degraded", "drain", "draining", "warning", "starting", "deploying", "unknown", ""].includes(normalized)
        ? "warning"
        : "outline";
  return <UiBadge variant={variant}>{label}</UiBadge>;
}
