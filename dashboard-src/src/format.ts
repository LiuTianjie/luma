import type { Lang } from "./types";

// Format a Unix (seconds) timestamp as a locale date-time string. Shared by the
// deploy/build history views and application config panels. Returns "-" for falsy
// timestamps. Pass `lang` to pin the locale; omit to use the browser default.
export function formatTimestamp(seconds?: number, lang?: Lang): string {
  if (!seconds) return "-";
  try {
    const locale = lang ? (lang === "zh" ? "zh-CN" : "en-US") : undefined;
    return new Date(seconds * 1000).toLocaleString(locale);
  } catch {
    return String(seconds);
  }
}
