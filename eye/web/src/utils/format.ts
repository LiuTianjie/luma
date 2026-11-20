const localFullFmt = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const localShortFmt = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const localHourFmt = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export function parseTimestampMs(raw?: string | number | null): number | null {
  if (raw === undefined || raw === null || raw === "") return null;
  if (typeof raw === "number") {
    return raw > 1e11 ? raw : raw * 1000;
  }
  const s = raw.trim();
  if (!s) return null;
  // ClickHouse format "YYYY-MM-DD HH:00:00" is UTC
  const normalized = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(s) ? s.replace(" ", "T") + "Z" : s;
  const ms = Date.parse(normalized);
  return Number.isFinite(ms) ? ms : null;
}

export function formatLocalTime(raw?: string | null): string {
  const ms = parseTimestampMs(raw);
  if (ms === null) return raw || "—";
  return localFullFmt.format(new Date(ms));
}

export function formatShortTime(raw?: string | null): string {
  const ms = parseTimestampMs(raw);
  if (ms === null) return raw || "—";
  return localShortFmt.format(new Date(ms));
}

export function formatHourLabel(raw?: string | null): string {
  const ms = parseTimestampMs(raw);
  if (ms === null) return raw || "";
  return localHourFmt.format(new Date(ms));
}

export function formatRelativeTime(raw?: string | null): string {
  const ms = parseTimestampMs(raw);
  if (ms === null) return "暂无";
  const diffSec = Math.round((Date.now() - ms) / 1000);
  if (diffSec < 5) return "刚刚";
  if (diffSec < 60) return `${diffSec} 秒前`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin} 分钟前`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr} 小时前`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay} 天前`;
}

export function formatDurationMs(ms?: number | null): string {
  if (!ms || ms <= 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const sec = ms / 1000;
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const rem = Math.round(sec % 60);
  return `${m}m ${rem}s`;
}

export function formatDurationSec(sec?: number | null): string {
  if (sec === undefined || sec === null || sec < 0) return "—";
  if (sec === 0) return "< 1s";
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const rem = sec % 60;
  if (m < 60) return rem > 0 ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export function formatOffsetMs(offsetMs: number): string {
  if (offsetMs <= 0) return "+0.0s";
  if (offsetMs < 1000) return `+${offsetMs}ms`;
  const sec = offsetMs / 1000;
  if (sec < 60) return `+${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const rem = (sec % 60).toFixed(0).padStart(2, "0");
  return `+${m}:${rem}`;
}

export function formatDeltaBeforeCrash(bcTs?: string, crashTs?: string): string {
  const bMs = parseTimestampMs(bcTs);
  const cMs = parseTimestampMs(crashTs);
  if (bMs === null) return "";
  if (cMs === null) return formatShortTime(bcTs);
  const delta = cMs - bMs;
  if (Math.abs(delta) <= 40) return "崩溃时刻";
  if (delta > 0) {
    if (delta < 1000) return `崩溃前 ${delta}ms`;
    if (delta < 60_000) return `崩溃前 ${(delta / 1000).toFixed(2)}s`;
    return `崩溃前 ${(delta / 60_000).toFixed(1)}m`;
  }
  const after = Math.abs(delta);
  return after < 1000 ? `+${after}ms` : `+${(after / 1000).toFixed(1)}s`;
}

export function formatBytes(bytes?: number | null): string {
  if (!bytes || bytes <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let val = bytes;
  let idx = 0;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx++;
  }
  return `${val >= 10 || idx === 0 ? val.toFixed(1) : val.toFixed(2)} ${units[idx]}`;
}

export function formatCompactNumber(n?: number | null): string {
  if (n === undefined || n === null) return "0";
  if (n < 10_000) return n.toLocaleString("en-US");
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

export function shortId(id?: string, max = 14): string {
  if (!id) return "匿名";
  if (id.length <= max) return id;
  return `${id.slice(0, max)}…`;
}
