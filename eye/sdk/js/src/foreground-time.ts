/**
 * Computes total foreground time in milliseconds using PerformanceTimeline VisibilityStateEntry,
 * falling back to performance.now() on browsers without visibility-state entries.
 */
export function getTotalForegroundTime(): number {
  if (typeof performance === "undefined") return 0;
  const entries = typeof performance.getEntriesByType === "function"
    ? (performance.getEntriesByType("visibility-state") as Array<{ name: string; startTime: number }>)
    : [];

  if (!entries || entries.length === 0) {
    return performance.now();
  }

  let total = 0;
  for (let i = 0; i < entries.length; i++) {
    if (entries[i].name === "visible") {
      const start = entries[i].startTime;
      const end = i + 1 < entries.length ? entries[i + 1].startTime : performance.now();
      if (end > start) {
        total += end - start;
      }
    }
  }
  return total;
}

export class ForegroundTracker {
  private lastSampleMs: number;

  constructor() {
    this.lastSampleMs = getTotalForegroundTime();
  }

  sampleDeltaMs(): number {
    const now = getTotalForegroundTime();
    const delta = Math.max(0, Math.round(now - this.lastSampleMs));
    this.lastSampleMs = now;
    return delta;
  }
}
