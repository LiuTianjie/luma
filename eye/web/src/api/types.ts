export interface RetentionStatus {
  retention_days: number;
  max_storage_bytes: number;
  total_used_bytes?: number;
  clickhouse_bytes?: number;
  sourcemap_bytes?: number;
  last_run_at?: string;
  dropped_partitions?: number;
  pruned_releases?: number;
  pruned_issues?: number;
  last_error?: string;
}

export interface AuthStatus {
  authenticated: boolean;
  auth_mode?: "control-sso" | "local";
  auth_method?: string;
  control_url?: string;
  control_security_url?: string;
  sso_login_url?: string;
  token_login_enabled?: boolean;
  mode: string;
  username?: string;
  display_name?: string;
  user_id?: string;
  has_password?: boolean;
  passkey_count?: number;
  passkeys_available?: boolean;
  rp_id?: string;
  session_ttl_days?: number;
  retention?: RetentionStatus;
}

export interface PasskeyCredential {
  id: string;
  name: string;
  aaguid: string;
  providerName?: string;
  transports: string[];
  credentialDeviceType: "singleDevice" | "multiDevice";
  credentialBackedUp: boolean;
  counter: number;
  registeredAt: number;
  lastUsedAt?: number;
}

export interface BrowserSession {
  id: string;
  method: "token" | "password" | "passkey" | string;
  passkeyName?: string;
  userAgent: string;
  createdAt: number;
  lastSeenAt: number;
  expiresAt: number;
  current: boolean;
}

export interface Project {
  dsn_id: number;
  slug: string;
  name: string;
  public_key: string;
  allowed_origins: string[];
  rate_limit_rps: number;
  created_at: string;
}

export interface PortfolioProjectStats {
  project: Project;
  active_users_5m: number;
  unique_visitors: number;
  pageviews: number;
  custom_events: number;
  sessions: number;
  error_count: number;
  open_issues: number;
  crash_free_rate: number;
  lcp_p75: number;
  inp_p75: number;
  cls_p75: number;
  last_event_at: string;
  sparkline: number[];
}

export interface PortfolioOverview {
  window: string;
  total_projects: number;
  active_projects: number;
  active_users_5m: number;
  unique_visitors: number;
  pageviews: number;
  custom_events: number;
  sessions: number;
  error_count: number;
  open_issues: number;
  overall_crash_free_rate: number;
  projects: PortfolioProjectStats[];
}

export interface DimensionCount {
  key: string;
  count: number;
  users?: number;
}

export interface TimeseriesPoint {
  hour: string;
  pageviews: number;
  events: number;
  visitors: number;
  errors: number;
}

export interface VitalSummary {
  metric_name: string;
  p75: number;
  samples: number;
}

export interface OverviewStats {
  project: string;
  window: string;
  active_users_5m: number;
  unique_visitors: number;
  pageviews: number;
  custom_events: number;
  sessions: number;
  error_count: number;
  crash_free_rate: number;
  timeseries: TimeseriesPoint[];
  top_pages: DimensionCount[];
  top_events: DimensionCount[];
  top_referrers: DimensionCount[];
  top_browsers: DimensionCount[];
  top_os: DimensionCount[];
  top_devices: DimensionCount[];
  top_countries: DimensionCount[];
  top_sources: DimensionCount[];
  top_mediums: DimensionCount[];
  top_campaigns: DimensionCount[];
  top_channels: DimensionCount[];
  top_platforms: DimensionCount[];
  top_languages: DimensionCount[];
  vitals_p75: VitalSummary[];
}

export interface PageMetricRow {
  page_path: string;
  pageviews: number;
  users: number;
  avg_duration_ms: number;
  p75_duration_ms: number;
  error_count: number;
  lcp_p75: number;
  inp_p75: number;
  cls_p75: number;
  fcp_p75: number;
  ttfb_p75: number;
  vital_samples: number;
}

export interface EventView {
  ts: string;
  event_type: string;
  event_name: string;
  actor_id: string;
  session_id: string;
  env: string;
  release: string;
  page_path: string;
  page_url: string;
  referrer: string;
  utm_source: string;
  utm_medium: string;
  utm_campaign: string;
  duration_ms: number;
  browser: string;
  os: string;
  device_type: string;
  country: string;
  trace_id: string;
  props?: Record<string, unknown>;
}

export interface SessionSummary {
  session_id: string;
  actor_id: string;
  start_ts: string;
  last_ts: string;
  duration_sec: number;
  pageviews: number;
  custom_events: number;
  total_events: number;
  error_count: number;
  log_count: number;
  entry_page: string;
  exit_page: string;
  browser: string;
  os: string;
  device_type: string;
  country: string;
  env: string;
  release: string;
  has_replay?: boolean;
}

export interface StackFrame {
  filename: string;
  function: string;
  lineno: number;
  colno: number;
  in_app: boolean;
  pre_context?: string[];
  context_line?: string;
  post_context?: string[];
  symbolicated?: boolean;
}

export interface Breadcrumb {
  ts?: string;
  category: string;
  level: string;
  message: string;
  data?: Record<string, unknown>;
}

export interface TimelineItem {
  id: string;
  kind: "pageview" | "custom" | "identify" | "lifecycle" | "vital" | "log" | "exception" | string;
  ts: string;
  ts_ms: number;
  offset_ms: number;
  session_id: string;
  actor_id: string;
  title: string;
  subtitle?: string;
  level?: string;
  page_path?: string;
  page_url?: string;
  duration_ms?: number;
  trace_id?: string;
  metric_name?: string;
  metric_value?: number;
  metric_rating?: string;
  fingerprint?: string;
  error_type?: string;
  error_message?: string;
  culprit?: string;
  props?: Record<string, unknown>;
  tags?: Record<string, string>;
  frames?: StackFrame[];
  breadcrumbs?: Breadcrumb[];
}

export interface TimelineResponse {
  project: string;
  actor_id: string;
  session_id: string;
  browser: string;
  os: string;
  device_type: string;
  country: string;
  env: string;
  release: string;
  start_ts: string;
  end_ts: string;
  duration_ms: number;
  total_items: number;
  error_count: number;
  has_replay?: boolean;
  items: TimelineItem[];
  user_sessions: SessionSummary[];
}

export interface SerializedDomNode {
  id: number;
  t: 0 | 1 | 2 | 3;
  tag?: string;
  a?: Record<string, string>;
  ch?: SerializedDomNode[];
  tx?: string;
  css?: string;
  blocked?: [number, number];
  box?: [number, number];
  svg?: 1;
}

export type ReplayEvent =
  | { t: 1; ts: number; href: string; w: number; h: number; title?: string }
  | { t: 2; ts: number; href: string; w: number; h: number; root: SerializedDomNode }
  | {
      t: 3;
      ts: number;
      rm?: number[];
      ad?: Array<{ p: number; nx: number | null; n: SerializedDomNode }>;
      tx?: Array<{ id: number; v: string }>;
      at?: Array<{ id: number; k: string; v: string | null }>;
    }
  | { t: 4; ts: number; pts: Array<[number, number, number]> }
  | { t: 5; ts: number; x: number; y: number; id?: number; tag?: string; label?: string }
  | { t: 6; ts: number; id: number; x: number; y: number }
  | { t: 7; ts: number; id: number; v?: string; c?: boolean }
  | { t: 8; ts: number; id?: number; css: string };

export interface SessionReplayResponse {
  project: string;
  session_id: string;
  actor_id: string;
  has_dom_replay: boolean;
  segment_count: number;
  events_count: number;
  start_ms: number;
  end_ms: number;
  duration_ms: number;
  viewport_w: number;
  viewport_h: number;
  page_url: string;
  events: ReplayEvent[];
}

export interface IssueSummary {
  project: string;
  fingerprint: string;
  status: "open" | "resolved" | "ignored";
  level: string;
  error_type: string;
  error_message: string;
  culprit: string;
  last_release: string;
  first_seen: string;
  last_seen: string;
  count: number;
  users: number;
}

export interface IssueOccurrence {
  ts: string;
  actor_id: string;
  session_id: string;
  env: string;
  release: string;
  level: string;
  page_url: string;
  trace_id: string;
  browser: string;
  os: string;
  device_type: string;
  frames: StackFrame[];
  breadcrumbs: Breadcrumb[];
  tags: Record<string, string>;
  extra?: Record<string, unknown>;
}

export interface IssueDetail {
  summary: IssueSummary;
  occurred_at: string;
  actor_id: string;
  session_id: string;
  env: string;
  release: string;
  page_url: string;
  trace_id: string;
  browser: string;
  os: string;
  device_type: string;
  frames: StackFrame[];
  breadcrumbs: Breadcrumb[];
  tags: Record<string, string>;
  extra?: Record<string, unknown>;
  occurrences: IssueOccurrence[];
}

export interface FunnelStepResult {
  step: string;
  step_index: number;
  users: number;
  conversion_rate: number;
  step_rate: number;
}

export interface FunnelResult {
  project: string;
  window_seconds: number;
  since: string;
  steps: FunnelStepResult[];
  overall_conversion: number;
}

export interface RetentionRow {
  cohort_date: string;
  cohort_users: number;
  day_counts: number[];
  day_rates: number[];
}

export interface RetentionResult {
  project: string;
  days: number;
  rows: RetentionRow[];
}

export interface LogView {
  ts: string;
  project: string;
  env: string;
  release: string;
  level: string;
  message: string;
  actor_id: string;
  session_id: string;
  page_url: string;
  trace_id: string;
  attrs?: Record<string, unknown>;
}
