import type { TraceSpan } from "./types";

export interface SpanRow {
  span: TraceSpan;
  depth: number;
  ancestors: string[];
  children: string[];
  offset: number;
  duration: number;
  self: number;
  orphan: boolean;
}

export function milliseconds(value: string, origin = "0") {
  return Number(BigInt(value) - BigInt(origin)) / 1e6;
}

export function spanDuration(ms: number) {
  if (ms === 0) return "0 ms";
  if (ms < 1) return `${(ms * 1000).toFixed(1)} μs`;
  if (ms < 1000) return `${ms.toFixed(ms < 10 ? 2 : 1)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

// 按父子关系稳定排列 Span；缺失父节点和环形数据保留为独立根节点。
export function buildTrace(spans: TraceSpan[]) {
  const byId = new Map(spans.map((span) => [span.spanId, span]));
  const children = new Map<string, TraceSpan[]>();
  const roots: TraceSpan[] = [];
  for (const span of spans) {
    if (span.parentSpanId !== span.spanId && byId.has(span.parentSpanId)) {
      const siblings = children.get(span.parentSpanId) || [];
      siblings.push(span);
      children.set(span.parentSpanId, siblings);
    } else roots.push(span);
  }
  const times = spans.map((span) => BigInt(span.startTimeUnixNano));
  const origin = times
    .reduce((a, b) => (a < b ? a : b), times[0] || 0n)
    .toString();
  const total = Math.max(
    0,
    ...spans.map((span) => milliseconds(span.endTimeUnixNano, origin)),
  );
  const seen = new Set<string>();
  const rows: SpanRow[] = [];
  const order = (a: TraceSpan, b: TraceSpan) =>
    milliseconds(a.startTimeUnixNano, b.startTimeUnixNano);
  roots.sort(order);
  const visit = (root: TraceSpan) => {
    const stack = [{ span: root, ancestors: [] as string[] }];
    while (stack.length) {
      const { span, ancestors } = stack.pop()!;
      if (seen.has(span.spanId)) continue;
      seen.add(span.spanId);
      const descendants = (children.get(span.spanId) || []).sort(order);
      const length = milliseconds(span.endTimeUnixNano, span.startTimeUnixNano);
      const intervals = descendants
        .map<[number, number]>((child) => [
          Math.max(
            0,
            milliseconds(child.startTimeUnixNano, span.startTimeUnixNano),
          ),
          Math.min(
            length,
            milliseconds(child.endTimeUnixNano, span.startTimeUnixNano),
          ),
        ])
        .filter(([start, end]) => end > start)
        .sort((a, b) => a[0] - b[0]);
      let covered = 0;
      let end = 0;
      for (const [start, stop] of intervals) {
        covered += Math.max(0, stop - Math.max(start, end));
        end = Math.max(end, stop);
      }
      rows.push({
        span,
        ancestors,
        depth: ancestors.length,
        children: descendants.map((child) => child.spanId),
        offset: milliseconds(span.startTimeUnixNano, origin),
        duration: length,
        self: Math.max(0, length - covered),
        orphan: Boolean(span.parentSpanId && !byId.has(span.parentSpanId)),
      });
      for (const child of [...descendants].reverse())
        stack.push({ span: child, ancestors: [...ancestors, span.spanId] });
    }
  };
  roots.forEach(visit);
  spans
    .filter((span) => !seen.has(span.spanId))
    .sort(order)
    .forEach(visit);
  return { rows, origin, total };
}
