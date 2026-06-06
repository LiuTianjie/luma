import { useId } from "react";
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

/** Inline mini trend, no axes. Fixed pixel size so points stay crisp. */
export function Sparkline({
  points,
  color = "var(--blue)",
  width = 104,
  height = 30,
  range,
}: {
  points: MetricPoint[];
  color?: string;
  width?: number;
  height?: number;
  range?: Range;
}) {
  const gradientId = useId();
  if (!points || points.length < 2) {
    return <span className="sparkline sparkline-empty" style={{ width, height }} aria-hidden />;
  }
  const pad = 3;
  const r = valueRange(points, range);
  const coords = project(points, width, height, pad, r);
  const [lastX, lastY] = coords[coords.length - 1];
  return (
    <svg className="sparkline" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-hidden>
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.28" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={toAreaPath(coords, width, height)} fill={`url(#${gradientId})`} stroke="none" />
      <path d={toLinePath(coords)} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={lastX} cy={lastY} r={2.4} fill={color} />
    </svg>
  );
}

/** Full trend with gridlines and axis labels. Scales with its container via
 *  a stretched viewBox; non-scaling-stroke keeps the line crisp at any width. */
export function TrendChart({
  points,
  color = "var(--blue)",
  range,
  format = (v) => v.toFixed(1),
  height = 150,
  emptyLabel = "no data",
}: {
  points: MetricPoint[];
  color?: string;
  range?: Range;
  format?: (value: number) => string;
  height?: number;
  emptyLabel?: string;
}) {
  const gradientId = useId();
  if (!points || points.length < 2) {
    return <div className="trend-chart trend-chart-empty" style={{ height }}>{emptyLabel}</div>;
  }
  const W = 720;
  const H = height;
  const padX = 6;
  const padY = 12;
  const r = valueRange(points, range);
  const coords = project(points, W - padX * 2, H, padY, r).map(([x, y]) => [x + padX, y] as const);
  const gridlines = [0, 0.25, 0.5, 0.75, 1].map((frac) => {
    const value = r.min + (r.max - r.min) * (1 - frac);
    const y = padY + frac * (H - padY * 2);
    return { y, value };
  });
  return (
    <svg
      className="trend-chart"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      role="img"
      aria-hidden
      style={{ height }}
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.22" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {gridlines.map((line, i) => (
        <g key={i}>
          <line
            x1={0}
            x2={W}
            y1={line.y}
            y2={line.y}
            stroke="var(--line)"
            strokeWidth={1}
            vectorEffect="non-scaling-stroke"
          />
          <text x={4} y={line.y - 3} className="trend-axis-label" fill="var(--muted)">
            {format(line.value)}
          </text>
        </g>
      ))}
      <path d={toAreaPath(coords, W, H - padY)} fill={`url(#${gradientId})`} stroke="none" />
      <path
        d={toLinePath(coords)}
        fill="none"
        stroke={color}
        strokeWidth={2}
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
