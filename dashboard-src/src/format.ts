import type { Lang } from "./types";

// Format a Unix (seconds) timestamp for the deploy/build history views.
// Returns "-" for falsy timestamps, which the history tests rely on.
export function formatTimestamp(seconds?: number, lang: Lang = "zh"): string {
  return seconds ? formatDateTime(seconds, lang) : "-";
}

// Show the immutable image identity compactly; keep the full reference in a tooltip.
export function formatImageIdentity(image?: string): string {
  if (!image) return "";
  const digest = image.match(/(?:@|^)sha256:([a-fA-F0-9]+)$/);
  if (digest) return `sha256:${digest[1].slice(0, 12)}`;
  const repository = image.slice(image.lastIndexOf("/") + 1);
  const colon = repository.lastIndexOf(":");
  return colon >= 0 ? repository.slice(colon + 1) : repository;
}

/** Placeholder for a value that is missing or not yet measured. */
export const EMPTY_VALUE = "—";

// Memory and disk sizes use binary units so node cards, metrics and detail pages
// report the same figure for the same byte count.
export function formatBytes(value?: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return EMPTY_VALUE;
  if (value === 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  const unit = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${Number((value / 1024 ** unit).toFixed(unit === 0 ? 0 : 1))} ${units[unit]}`;
}

export function formatPercent(value?: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return EMPTY_VALUE;
  return `${value >= 10 ? Math.round(value) : Number(value.toFixed(1))}%`;
}

const DATE_TIME_OPTIONS: Intl.DateTimeFormatOptions = { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };

/** One date-time style for the whole console. Accepts Unix seconds, milliseconds or a Date. */
export function formatDateTime(value: number | string | Date | null | undefined, lang: Lang): string {
  if (value === null || value === undefined || value === "") return EMPTY_VALUE;
  const date = value instanceof Date ? value
    : typeof value === "number" ? new Date(value < 1e12 ? value * 1000 : value)
    : new Date(value);
  if (Number.isNaN(date.getTime())) return EMPTY_VALUE;
  return date.toLocaleString(lang === "zh" ? "zh-CN" : "en-US", DATE_TIME_OPTIONS);
}
