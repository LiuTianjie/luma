import { Check, ChevronDown } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

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
  const id = useId();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [open, setOpen] = useState(false);
  const selected = options.find((option) => option.value === value);
  const enabledOptions = useMemo(() => options.filter((option) => !option.disabled), [options]);
  const currentIndex = Math.max(0, enabledOptions.findIndex((option) => option.value === value));

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("pointerdown", close);
    return () => window.removeEventListener("pointerdown", close);
  }, [open]);

  const choose = (option: SelectOption) => {
    if (option.disabled) return;
    onChange(option.value);
    setOpen(false);
  };

  const move = (delta: number) => {
    if (!enabledOptions.length) return;
    const next = enabledOptions[(currentIndex + delta + enabledOptions.length) % enabledOptions.length];
    onChange(next.value);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "Escape") {
      setOpen(false);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setOpen((current) => !current);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!open) setOpen(true);
      move(1);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) setOpen(true);
      move(-1);
    }
  };

  return (
    <div ref={rootRef} className={`luma-select ${open ? "open" : ""} ${disabled ? "disabled" : ""} ${className}`.trim()}>
      <button
        type="button"
        className="luma-select-trigger"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={`${id}-listbox`}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={onKeyDown}
      >
        <span className={selected ? "luma-select-value" : "luma-select-value placeholder"}>{selected?.label || placeholder}</span>
        <ChevronDown size={15} aria-hidden="true" />
      </button>
      {open ? (
        <div id={`${id}-listbox`} className="luma-select-menu" role="listbox" aria-label={ariaLabel}>
          {options.length ? options.map((option) => {
            const active = option.value === value;
            return (
              <button
                type="button"
                key={option.value || "__empty"}
                role="option"
                aria-selected={active}
                className={active ? "luma-select-option active" : "luma-select-option"}
                disabled={option.disabled}
                onClick={() => choose(option)}
              >
                <span>{option.label}</span>
                {active ? <Check size={14} aria-hidden="true" /> : null}
              </button>
            );
          }) : (
            <div className="luma-select-empty">{placeholder}</div>
          )}
        </div>
      ) : null}
    </div>
  );
}

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
  const normalized = (value || "").toLowerCase();
  const kind = ["ready", "running", "healthy", "active", "succeeded", "stable", "available"].includes(normalized)
    ? "good"
    : ["failed", "missing", "bad", "down", "error", "failed_partial", "critical"].includes(normalized)
      ? "danger"
      : ["pending", "degraded", "drain", "draining", "warning", "starting", "deploying", "unknown", ""].includes(normalized)
        ? "warn"
        : "warn";
  return <span className={`badge ${kind}`}>{label}</span>;
}
