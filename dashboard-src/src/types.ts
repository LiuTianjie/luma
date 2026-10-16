export type Lang = "zh" | "en";

export type Readiness = {
  dns?: {
    ready?: boolean;
    provider?: string;
    zone?: string;
    zoneIdConfigured?: boolean;
    target?: string;
  };
  nomad?: {
    available?: boolean;
    engine?: string;
    leader?: string;
  };
  setup?: Record<string, {
    configured?: boolean;
    required?: boolean;
    missing?: string[];
  }>;
  setupLastCheck?: {
    checkedAt?: number;
    checks?: Record<string, { status?: string; required?: boolean; detail?: string }>;
  };
};

export type NodeMetrics = {
  cpuPercent?: number;
  cpuCount?: number;
  load1?: number;
  loadPercent?: number;
  memoryTotalBytes?: number;
  memoryAvailableBytes?: number;
  memoryUsedPercent?: number;
  diskTotalBytes?: number;
  diskAvailableBytes?: number;
  diskUsedPercent?: number;
  inodesUsedPercent?: number;
  metricsPath?: string;
};

export type ResourceValues = {
  cpus?: number;
  memoryBytes?: number;
};

export type ActualResourceValues = {
  containers?: number;
  cpuPercent?: number;
  memoryUsageBytes?: number;
  memoryLimitBytes?: number;
  memoryPercent?: number;
  nodes?: string[];
};

export type DashboardTask = {
  id?: string;
  node?: string;
  region?: string;
  nodeAddress?: string;
  state?: string;
  desiredState?: string;
  containerId?: string;
  message?: string;
  error?: string;
  cpuPercent?: number;
  memoryUsageBytes?: number;
  memoryPercent?: number;
};

export type DashboardIssue = {
  severity?: "critical" | "warning" | "info" | string;
  kind?: string;
  target?: string;
  message?: string;
};

export type DashboardNode = {
  name?: string;
  displayName?: string;
  hostname?: string;
  region?: string;
  role?: string;
  state?: string;
  availability?: string;
  leader?: boolean;
  agentStatus?: string;
  agentOs?: string;
  agentVersion?: string;
  agentLastSeen?: number;
  storageCapabilities?: string[];
  terminalConnected?: boolean;
  terminalStatus?: string;
  metrics?: NodeMetrics;
  capacity?: ResourceValues;
};

export type DashboardNodeJoin = {
  token?: string;
  domain?: string;
};

export type DashboardBuildNode = {
  name?: string;
  region?: string;
  agentStatus?: string;
  agentOs?: string;
  nativePlatform?: string;
  storageCapabilities?: string[];
  ready?: boolean;
};

export type DashboardService = {
  name?: string;
  fullName?: string;
  managedBy?: string;
  stack?: string;
  region?: string;
  node?: string;
  exposure?: string;
  domain?: string;
  targetPort?: string;
  publishPort?: string;
  routeId?: string;
  network?: string;
  image?: string;
  status?: string;
  upstreams?: string[];
  running?: number;
  desired?: number;
  pending?: number;
  failed?: number;
  health?: string;
  nodes?: string[];
  storage?: DashboardVolume[];
  resources?: {
    limits?: ResourceValues;
    reservations?: ResourceValues;
    actual?: ActualResourceValues;
  };
  tasks?: DashboardTask[];
  diagnostics?: string[];
};

export type ServiceVersion = {
  version?: number | string;
  stable?: boolean;
  submitTime?: number | string;
  image?: string;
};

export type ServiceHistoryPayload = {
  clusterId?: string;
  service?: string;
  slug?: string;
  versions?: ServiceVersion[];
};

export type ServiceRollbackPayload = {
  clusterId?: string;
  service?: string;
  slug?: string;
  message?: string;
};

export type DashboardVolume = {
  name?: string;
  kind?: string;
  storageClass?: string;
  node?: string;
  endpoint?: string;
  networkPath?: string;
  services?: string[];
};

export type DashboardStorageClass = {
  name?: string;
  provider?: string;
  mode?: string;
  node?: string;
  path?: string;
  endpoint?: string;
  regions?: string[];
  nodes?: string[];
};

export type TrafficPath = {
  id?: string;
  domain?: string;
  kind?: string;
  segments?: string[];
  certificateRetry?: {
    available?: boolean;
    mode?: string;
    routeId?: string;
    certResolver?: string;
    reason?: string;
  };
  destinations?: TrafficDestination[];
};

export type TrafficDestination = {
  service?: string;
  region?: string;
  node?: string;
  nodeAddress?: string;
  address?: string;
  state?: string;
};

export type DashboardRegion = {
  name: string;
  builtin?: boolean;
  egress?: "proxy" | "direct" | string;
  exposures?: string[];
  createdAt?: number;
};

export type DashboardPayload = {
  scope?: string;
  applicationPage?: {
    offset: number; limit: number; total: number; hasMore: boolean;
    counts: { total: number; healthy: number; degraded: number; failed: number };
    statuses: string[]; regions: string[];
  };
  nodeJoin?: DashboardNodeJoin;
  cluster?: {
    id?: string;
    version?: string;
  };
  readiness?: Readiness;
  nodes?: DashboardNode[];
  regions?: DashboardRegion[];
  services?: DashboardService[];
  trafficPaths?: TrafficPath[];
  storage?: {
    storageClasses?: DashboardStorageClass[];
    volumes?: DashboardVolume[];
    warnings?: string[];
  };
  build?: {
    defaultNode?: string;
    registryHost?: string;
    pushHost?: string;
    nodes?: DashboardBuildNode[];
  };
  issues?: DashboardIssue[];
  errors?: string[];
};

export type SyncStatus = "notConnected" | "refreshing" | "updated" | "unavailable" | "tokenRejected";

export type MetricPoint = [number, number];

export type MetricSeries = Record<string, MetricPoint[]>;

export type MetricsHistoryPayload = {
  requestedWindow?: number;
  retentionSeconds?: number;
  sampleIntervalSeconds?: number;
  availableFrom?: number | null;
  latestSampleAt?: number | null;
  kind?: "node" | "service";
  name?: string;
  window?: number;
  series?: MetricSeries;
  updatedAt?: number;
};

export type BuildStepTiming = {
  stepId: string;
  name: string;
  durationMs: number;
  cached: boolean;
  startedAt?: number;
  completedAt?: number;
  layerBytes?: number;
};

export type ZotLayerMetadata = {
  digest: string;
  sizeBytes: number;
  command?: string;
  mediaType?: string;
};

export type ServiceImageArtifact = {
  serviceName: string;
  repository: string;
  digest: string;
  tag: string;
  platform: string;
  sizeBytes?: number;
  cacheHitRatio?: number;
  stepTimings?: BuildStepTiming[];
  layers?: ZotLayerMetadata[];
  immutableRef?: string;
};

export type BuildArtifact = {
  artifactId: string;
  project: string;
  sourceRepo: string;
  commitSha: string;
  branch: string;
  manifestKind: "service" | "compose";
  images: Record<string, ServiceImageArtifact>;
  createdAt: number;
  buildDurationMs?: number;
  builderNode?: string;
  cacheHitRatio?: number;
  totalSizeBytes?: number;
  gitContexts?: Record<string, Record<string, string>>;
};

export type ReleaseDiff = {
  previousReleaseId?: string | null;
  currentReleaseId: string;
  previousArtifactId?: string | null;
  currentArtifactId: string;
  imageChanges: Record<string, { from: string | null; to: string | null }>;
  replicaChanges: Record<string, { from: number; to: number }>;
  secretKeysAdded: string[];
  secretKeysRemoved: string[];
  envDigestChanged: boolean;
};

export type ReleaseRecord = {
  releaseId: string;
  project: string;
  environment: string;
  generation: number;
  artifactId: string;
  status: string;
  replicasByService: Record<string, number>;
  envDigest?: string;
  secretKeys?: string[];
  mountedSecretFiles?: Record<string, string>;
  previousReleaseId?: string;
  createdAt: number;
  updatedAt: number;
  message?: string;
  gatewayProbeOk?: boolean;
  diff?: ReleaseDiff;
};

export type NodeSelfHealStatus = {
  nodeName: string;
  registriesYamlConverged: boolean;
  noProxyAligned: boolean;
  zotDirectReachable: boolean;
  buildkitCacheBytes: number;
  buildkitCacheLimitBytes: number;
  lastFstrimAt: number;
  healthy: boolean;
  repairedActions?: string[];
};
