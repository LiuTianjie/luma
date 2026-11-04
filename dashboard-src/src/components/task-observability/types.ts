export interface TaskEvent {
  event: string;
  timestamp: number | string;
  task_name?: string;
  task_id?: string;
  trace_id?: string;
  span_id?: string;
  runtime?: string;
  queue?: string;
  retry?: number;
  outcome?: string;
  business_outcome?: string;
  business_status?: string;
  business_kind?: string;
  business_id?: string;
  elapsed_seconds?: number;
  stage?: string;
  progress_at?: number;
  error_type?: string;
  error_message?: string;
  error_location?: string;
  error_truncated?: boolean;
  phase_started_at?: number;
  phase_finished_at?: number;
  phase_duration_ms?: number;
  phase_count?: number;
  phase_outcome?: string;
  phase_omitted?: number;
}

export interface EventsResult {
  state: string;
  items: TaskEvent[];
  until?: number;
  next_offset?: number | null;
  possibly_truncated?: boolean;
  related_truncated?: boolean;
}

export interface TraceSpan {
  spanId: string;
  parentSpanId: string;
  name: string;
  service: string;
  kind: string;
  status: "ok" | "error" | "unset";
  startTimeUnixNano: string;
  endTimeUnixNano: string;
  attributes: Record<string, string>;
  resource: Record<string, string>;
  events: {
    name: string;
    timeUnixNano: string;
    attributes: Record<string, string>;
  }[];
  links: { traceId: string; spanId: string }[];
  droppedAttributes: number;
  detailsTruncated: boolean;
}

export interface TraceDetail {
  state: string;
  traceId: string;
  spans: TraceSpan[];
  partial?: boolean;
  truncated?: boolean;
}

export interface TracesResult {
  state: string;
  possibly_truncated?: boolean;
  items: {
    traceID: string;
    rootServiceName: string;
    rootTraceName: string;
    startTimeUnixNano: string;
    durationMs: number;
  }[];
}

export interface LogsResult {
  state: string;
  context?: boolean;
  possibly_truncated?: boolean;
  diagnostic?: {
    description: string;
    error_type: string;
    error_message: string;
    stacktrace: string;
    truncated: boolean;
  };
  items: {
    timestamp: string;
    line: string;
    task: string;
    stream: string;
    matched: boolean;
  }[];
}

export interface MetricSource {
  state: string;
  rows: { metric: Record<string, string>; value: [number, string] }[];
}
export interface OverviewResult {
  state: string;
  sources?: Record<string, MetricSource>;
  sampled_at?: number;
}
export type Query = Record<string, string | number>;
export type BoardProps = { app: string; token: string; zh: boolean };
