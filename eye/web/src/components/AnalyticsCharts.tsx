import { useState } from "react";
import { Area, AreaChart, CartesianGrid, XAxis, YAxis } from "recharts";
import type { TimeseriesPoint } from "@/api/types";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Empty, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { cn } from "@/lib/utils";
import { formatHourLabel } from "@/utils/format";

export function Sparkline({
  values,
  tone = "normal",
  width = 104,
  height = 30,
}: {
  values: number[];
  tone?: "normal" | "critical";
  width?: number;
  height?: number;
}) {
  const safe = values && values.length > 0 ? values : [0, 0];
  const data = safe.map((v, idx) => ({ idx, value: Number.isFinite(v) ? v : 0 }));
  const peak = Math.max(1, ...data.map((d) => d.value));
  const color = tone === "critical" ? "var(--destructive)" : "var(--chart-1)";
  const config = { value: { color } } satisfies ChartConfig;

  return (
    <ChartContainer
      config={config}
      className="shrink-0"
      style={{ width, height }}
      aria-hidden="true"
    >
      <AreaChart
        data={data}
        margin={{ top: 2, right: 2, bottom: 2, left: 2 }}
        accessibilityLayer={false}
      >
        <XAxis dataKey="idx" type="number" domain={["dataMin", "dataMax"]} hide />
        <YAxis domain={[0, peak * 1.1]} hide />
        <Area
          dataKey="value"
          type="monotone"
          fill="var(--color-value)"
          fillOpacity={0.14}
          stroke="var(--color-value)"
          strokeWidth={1.5}
          dot={false}
          isAnimationActive={false}
        />
      </AreaChart>
    </ChartContainer>
  );
}

const SERIES_CONFIG = {
  pageviews: { label: "页面浏览 (PV)", color: "var(--chart-1)" },
  visitors: { label: "独立访客 (UV)", color: "var(--chart-2)" },
  events: { label: "自定义事件", color: "var(--chart-3)" },
  errors: { label: "异常报错", color: "var(--destructive)" },
} satisfies ChartConfig;

type SeriesKey = keyof typeof SERIES_CONFIG;

export function TrafficTrendChart({ points, height = 240 }: { points: TimeseriesPoint[]; height?: number }) {
  const [enabled, setEnabled] = useState<Record<SeriesKey, boolean>>({
    pageviews: true,
    visitors: true,
    events: true,
    errors: true,
  });

  if (!points || points.length === 0) {
    return (
      <Empty className="py-10">
        <EmptyHeader>
          <EmptyTitle className="text-sm font-normal text-muted-foreground">
            所选时间窗口内暂无时序上报数据
          </EmptyTitle>
        </EmptyHeader>
      </Empty>
    );
  }

  const data = points.map((p) => ({
    ...p,
    label: formatHourLabel(p.hour),
  }));

  const toggle = (key: SeriesKey) => {
    setEnabled((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="图表序列过滤">
        {(Object.keys(SERIES_CONFIG) as SeriesKey[]).map((key) => {
          const item = SERIES_CONFIG[key];
          const active = enabled[key];
          const total = points.reduce((acc, p) => acc + (p[key] || 0), 0);
          return (
            <button
              key={key}
              type="button"
              aria-pressed={active}
              onClick={() => toggle(key)}
              className={cn(
                "inline-flex items-center gap-2 rounded-md px-2.5 py-1 text-xs ring-1 ring-border transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring",
                active ? "bg-card text-foreground" : "bg-muted/40 text-muted-foreground opacity-60",
              )}
            >
              <span
                className="size-2 rounded-full"
                style={{ backgroundColor: item.color }}
                aria-hidden="true"
              />
              <span>{item.label}</span>
              <strong className="font-medium tabular-nums">{total.toLocaleString()}</strong>
            </button>
          );
        })}
      </div>

      <ChartContainer config={SERIES_CONFIG} className="w-full" style={{ height }}>
        <AreaChart accessibilityLayer data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} />
          <XAxis
            dataKey="label"
            tickLine={false}
            axisLine={false}
            tickMargin={8}
            minTickGap={36}
          />
          <YAxis tickLine={false} axisLine={false} tickMargin={8} width={44} />
          <ChartTooltip content={<ChartTooltipContent />} />
          {enabled.pageviews && (
            <Area
              dataKey="pageviews"
              type="monotone"
              fill="var(--color-pageviews)"
              fillOpacity={0.12}
              stroke="var(--color-pageviews)"
              strokeWidth={1.75}
              dot={false}
              isAnimationActive={false}
            />
          )}
          {enabled.visitors && (
            <Area
              dataKey="visitors"
              type="monotone"
              fill="var(--color-visitors)"
              fillOpacity={0.08}
              stroke="var(--color-visitors)"
              strokeWidth={1.5}
              dot={false}
              isAnimationActive={false}
            />
          )}
          {enabled.events && (
            <Area
              dataKey="events"
              type="monotone"
              fill="var(--color-events)"
              fillOpacity={0.06}
              stroke="var(--color-events)"
              strokeWidth={1.5}
              dot={false}
              isAnimationActive={false}
            />
          )}
          {enabled.errors && (
            <Area
              dataKey="errors"
              type="monotone"
              fill="var(--color-errors)"
              fillOpacity={0.18}
              stroke="var(--color-errors)"
              strokeWidth={1.75}
              dot={false}
              isAnimationActive={false}
            />
          )}
        </AreaChart>
      </ChartContainer>
    </div>
  );
}
