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

// Show the immutable image identity compactly; keep the full reference in a tooltip.
export function formatImageIdentity(image?: string): string {
  if (!image) return "";
  const digest = image.match(/(?:@|^)sha256:([a-fA-F0-9]+)$/);
  if (digest) return `sha256:${digest[1].slice(0, 12)}`;
  const repository = image.slice(image.lastIndexOf("/") + 1);
  const colon = repository.lastIndexOf(":");
  return colon >= 0 ? repository.slice(colon + 1) : repository;
}
