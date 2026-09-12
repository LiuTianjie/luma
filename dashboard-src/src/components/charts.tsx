import { Area, AreaChart, CartesianGrid, XAxis, YAxis } from "recharts";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Empty, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import type { MetricPoint } from "../types";

type Range = { min: number; max: number };
type ChartPoint = { timestamp: number; value: number | null };

function chartPoints(
  points: MetricPoint[],
  maxGapSeconds: number,
): ChartPoint[] {
  const result: ChartPoint[] = [];
  for (const [timestamp, value] of points) {
    if (!Number.isFinite(timestamp) || !Number.isFinite(value)) continue;
    const previous = result[result.length - 1];
    if (previous && timestamp - previous.timestamp > maxGapSeconds) {
      result.push({
        timestamp: previous.timestamp + (timestamp - previous.timestamp) / 2,
        value: null,
      });
    }
    result.push({ timestamp, value });
  }
  return result;
}

function valueRange(points: MetricPoint[], explicit?: Range): Range {
  if (explicit) return explicit;
  const peak = Math.max(
    0,
    ...points.map((point) => (Number.isFinite(point[1]) ? point[1] : 0)),
  );
  const rawStep = ((peak || 1) * 1.05) / 4;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const step =
    ([1, 2, 2.5, 5, 10].find((value) => value * magnitude >= rawStep) ?? 10) *
    magnitude;
  return { min: 0, max: step * 4 };
}

/** Decorative inline trend; it uses the same chart tokens as the full plots. */
export function Sparkline({
  points,
  color = "var(--chart-1)",
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
  const bounds = valueRange(points || [], range);
  const config = { value: { color } } satisfies ChartConfig;
  return (
    <ChartContainer
      config={config}
      className="shrink-0"
      style={{ width, height }}
      aria-hidden="true"
    >
      <AreaChart
        data={chartPoints(points || [], maxGapSeconds)}
        margin={{ top: 3, right: 2, bottom: 3, left: 2 }}
        accessibilityLayer={false}
      >
        <XAxis
          dataKey="timestamp"
          type="number"
          domain={["dataMin", "dataMax"]}
          hide
        />
        <YAxis domain={[bounds.min, bounds.max]} hide />
        <Area
          dataKey="value"
          type="linear"
          fill="var(--color-value)"
          fillOpacity={0.15}
          stroke="var(--color-value)"
          strokeWidth={1.5}
          connectNulls={false}
          dot={false}
          isAnimationActive={false}
        />
      </AreaChart>
    </ChartContainer>
  );
}

/** Numeric time axis and null samples preserve the real spacing of missing data. */
export function TrendChart({
  points,
  color = "var(--chart-1)",
  range,
  format = (value) => value.toFixed(1),
  height = 160,
  emptyLabel = "No data",
  maxGapSeconds = Infinity,
  label = "",
  locale = "en",
}: {
  points: MetricPoint[];
  color?: string;
  range?: Range;
  format?: (value: number) => string;
  height?: number;
  emptyLabel?: string;
  maxGapSeconds?: number;
  label?: string;
  locale?: string;
}) {
  const data = chartPoints(points, maxGapSeconds);
  const samples = data.filter((point) => point.value !== null);
  const bounds = valueRange(points, range);
  const config = { value: { label, color } } satisfies ChartConfig;
  const time = (timestamp: number) =>
    new Date(timestamp * 1000).toLocaleTimeString(locale, {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  const stamp = (timestamp: number) =>
    new Date(timestamp * 1000).toLocaleString(locale, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  if (samples.length < 2)
    return (
      <Empty style={{ height }}>
        <EmptyHeader>
          <EmptyTitle>{emptyLabel}</EmptyTitle>
        </EmptyHeader>
      </Empty>
    );
  return (
    <ChartContainer
      config={config}
      className="w-full"
      style={{ height }}
      aria-label={`${label}. ${time(samples[0].timestamp)}–${time(samples[samples.length - 1].timestamp)}. ${format(samples[samples.length - 1].value!)}`}
    >
      <AreaChart
        accessibilityLayer
        data={data}
        margin={{ top: 12, right: 24, left: 0, bottom: 0 }}
      >
        <CartesianGrid vertical={false} />
        <XAxis
          dataKey="timestamp"
          type="number"
          domain={["dataMin", "dataMax"]}
          tickLine={false}
          axisLine={false}
          tickMargin={8}
          tickFormatter={time}
          minTickGap={40}
        />
        <YAxis
          domain={[bounds.min, bounds.max]}
          tickCount={5}
          tickLine={false}
          axisLine={false}
          tickMargin={8}
          width={64}
          tickFormatter={format}
        />
        <ChartTooltip
          content={(props) => (
            <ChartTooltipContent
              active={props.active}
              label={props.label}
              labelFormatter={(_, payload) =>
                stamp(Number(payload[0]?.payload?.timestamp))
              }
              payload={props.payload?.map((item) => ({
                ...item,
                value:
                  typeof item.value === "number"
                    ? format(item.value)
                    : item.value,
              }))}
            />
          )}
        />
        <Area
          dataKey="value"
          type="linear"
          fill="var(--color-value)"
          fillOpacity={0.15}
          stroke="var(--color-value)"
          strokeWidth={2}
          connectNulls={false}
          dot={{ r: 1.5 }}
          isAnimationActive={false}
        />
      </AreaChart>
    </ChartContainer>
  );
}
