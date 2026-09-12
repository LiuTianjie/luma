import { useEffect, useId, useRef, useState } from "react";
import type { MetricPoint } from "../types";

type Range = { min: number; max: number };

function valueRange(points: MetricPoint[], explicit?: Range): Range {
  if (explicit) return explicit;
  let max = 0;
  for (const [, value] of points) {
    if (value > max) max = value;
  }
  // Headroom so the peak never kisses the top edge.
  return { min: 0, max: max <= 0 ? 1 : max * 1.15 };
}

function project(points: MetricPoint[], width: number, height: number, pad: number, range: Range) {
  const first = points[0][0];
  const last = points[points.length - 1][0];
  const span = Math.max(1, last - first);
  const spread = Math.max(1e-9, range.max - range.min);
  const usable = height - pad * 2;
  return points.map(([ts, value]) => {
    const x = ((ts - first) / span) * width;
    const y = height - pad - ((value - range.min) / spread) * usable;
    return [x, Number.isFinite(y) ? y : height - pad] as const;
  });
}

function toLinePath(coords: ReadonlyArray<readonly [number, number]>): string {
  return coords.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`).join(" ");
}

function toAreaPath(coords: ReadonlyArray<readonly [number, number]>, width: number, height: number): string {
  if (!coords.length) return "";
  const line = toLinePath(coords);
  const lastX = coords[coords.length - 1][0];
  const firstX = coords[0][0];
  return `${line} L${lastX.toFixed(2)} ${height} L${firstX.toFixed(2)} ${height} Z`;
}

function splitAtGaps(
  points: MetricPoint[],
  coords: ReadonlyArray<readonly [number, number]>,
  maxGapSeconds: number,
) {
  const segments: Array<Array<readonly [number, number]>> = [];
  coords.forEach((coord, index) => {
    if (!index || points[index][0] - points[index - 1][0] > maxGapSeconds) segments.push([]);
    segments[segments.length - 1].push(coord);
  });
  return segments;
}

/** Inline mini trend, no axes. Fixed pixel size so points stay crisp. */
export function Sparkline({
  points,
  color = "var(--chart-line, #90baff)",
  width = 104,
  height = 30,
  range,
  maxGapSeconds = Infinity,
}: {
  points: MetricPoint[];
  color?: string;
  width?: number;
  height?: number;
  range?: Range;
  maxGapSeconds?: number;
}) {
  const gradientId = useId();
  if (!points || points.length < 2) {
    return <span className="sparkline sparkline-empty" style={{ width, height }} aria-hidden />;
  }
  const pad = 3;
  const r = valueRange(points, range);
  const coords = project(points, width, height, pad, r);
  const [lastX, lastY] = coords[coords.length - 1];
  const segments = splitAtGaps(points, coords, maxGapSeconds);
  return (
    <svg className="sparkline" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-hidden>
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.28" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {segments.map((segment, index) => <g key={index}>
        <path d={toAreaPath(segment, width, height)} fill={`url(#${gradientId})`} stroke="none" />
        <path d={toLinePath(segment)} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      </g>)}
      <circle cx={lastX} cy={lastY} r={2.4} fill={color} />
    </svg>
  );
}

/** Plot at its rendered width so axis text never stretches. */
export function TrendChart({ points, color = "var(--chart-line, #90baff)", range,
  format = (v) => v.toFixed(1), height = 160, emptyLabel = "No data",
  maxGapSeconds = Infinity, label = "", locale = "en",
}: {
  points: MetricPoint[]; color?: string; range?: Range;
  format?: (value: number) => string; height?: number; emptyLabel?: string;
  maxGapSeconds?: number; label?: string; locale?: string;
}) {
  const gradientId = useId();
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(720);
  const [active, setActive] = useState<number | null>(null);
  useEffect(() => {
    if (!container.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(1, entry.contentRect.width)));
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  const axisRange = range ?? (() => {
    const peak = Math.max(0, ...points.map((point) => point[1]));
    const rawStep = (peak || 1) * 1.05 / 4;
    const magnitude = 10 ** Math.floor(Math.log10(rawStep));
    const step = ([1, 2, 2.5, 5, 10].find((value) => value * magnitude >= rawStep) ?? 10) * magnitude;
    return { min: 0, max: step * 4 };
  })();
  const W = width, left = 62, right = 16, top = 14, bottom = height - 28;
  const plotWidth = Math.max(1, W - left - right);
  const r = axisRange;
  const first = points[0]?.[0] ?? 0, last = points[points.length - 1]?.[0] ?? first;
  const span = Math.max(1, last - first);
  const coords = points.map(([ts, value]) => [left + (ts - first) / span * plotWidth,
    bottom - (value - r.min) / Math.max(1e-9, r.max - r.min) * (bottom - top)] as const);
  const segments = splitAtGaps(points, coords, maxGapSeconds);
  const index = active === null ? null : Math.min(active, points.length - 1);
  const sample = index === null ? null : points[index];
  const coordinate = index === null ? null : coords[index];
  const time = (ts: number) => new Date(ts * 1000).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", hour12: false });
  return <div ref={container} className="metric-plot" style={{ minHeight: height }}>
    {points.length < 2 ? <div className="trend-chart-empty" style={{ height }}>{emptyLabel}</div> : <>
      <svg className="trend-chart" width="100%" height={height} viewBox={`0 0 ${W} ${height}`}
        tabIndex={0} role="img" aria-label={`${label}. ${time(first)}–${time(last)}. ${sample ? format(sample[1]) : format(points[points.length - 1][1])}`}
        onPointerMove={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          const ts = first + ((event.clientX - rect.left) / rect.width * W - left) / plotWidth * span;
          let nearest = 0;
          points.forEach(([value], i) => { if (Math.abs(value - ts) < Math.abs(points[nearest][0] - ts)) nearest = i; });
          setActive(nearest);
        }}
        onPointerLeave={() => setActive(null)} onFocus={() => setActive(points.length - 1)} onBlur={() => setActive(null)}
        onKeyDown={(event) => {
          if (event.key === "Escape") setActive(null);
          if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
            event.preventDefault();
            setActive(Math.max(0, Math.min(points.length - 1, (active ?? points.length - 1) + (event.key === "ArrowLeft" ? -1 : 1))));
          }
        }}>
        <defs><linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.16" />
          <stop offset="100%" stopColor={color} stopOpacity="0.015" />
        </linearGradient></defs>
        {[0, .25, .5, .75, 1].map((fraction) => {
          const y = top + fraction * (bottom - top);
          return <g key={fraction}>
            <line x1={left} x2={W - right} y1={y} y2={y} stroke="var(--border)" strokeDasharray="3 3" />
            <text x={left - 10} y={y} dy=".35em" textAnchor="end" className="trend-axis-label">{format(r.min + (r.max - r.min) * (1 - fraction))}</text>
          </g>;
        })}
        {Array.from({ length: W < 440 ? 3 : 5 }, (_, i) => i).map((i, _, ticks) => {
          const fraction = i / (ticks.length - 1), x = left + fraction * plotWidth;
          return <g key={i}><line x1={x} x2={x} y1={bottom} y2={bottom + 4} stroke="var(--border)" />
            <text x={x} y={height - 6} textAnchor={i === 0 ? "start" : i === ticks.length - 1 ? "end" : "middle"} className="trend-axis-label">{time(first + fraction * span)}</text></g>;
        })}
        {segments.map((segment, i) => <g key={i}>
          <path d={toAreaPath(segment, W, bottom)} fill={`url(#${gradientId})`} />
          <path d={toLinePath(segment)} fill="none" stroke={color} strokeWidth={1.8} strokeLinejoin="round" strokeLinecap="round" />
          {segment.length === 1 && <circle cx={segment[0][0]} cy={segment[0][1]} r={2} fill={color} />}
        </g>)}
        {coordinate && <g><line x1={coordinate[0]} x2={coordinate[0]} y1={top} y2={bottom} stroke="var(--muted-foreground)" strokeOpacity=".35" />
          <circle cx={coordinate[0]} cy={coordinate[1]} r={4} fill={color} stroke="var(--card)" strokeWidth={2} /></g>}
      </svg>
      {sample && coordinate && <div className="metric-tooltip" style={{ left: Math.max(0, Math.min(W - 184, coordinate[0] + 12)), top: 18 }}>
        <time>{new Date(sample[0] * 1000).toLocaleString(locale, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}</time>
        <div><span><i style={{ background: color }} />{label}</span><strong>{format(sample[1])}</strong></div>
      </div>}
    </>}
  </div>;
}
