export type Lang = "zh" | "en";

export type Readiness = {
  dns?: {
    ready?: boolean;
    provider?: string;
    zone?: string;
    target?: string;
  };
  portainer?: {
    ready?: boolean;
    apiConfigured?: boolean;
    endpointConfigured?: boolean;
  };
  swarm?: {
    available?: boolean;
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
  region?: string;
  role?: string;
  state?: string;
  availability?: string;
  leader?: boolean;
  agentStatus?: string;
  agentOs?: string;
  agentLastSeen?: number;
  storageCapabilities?: string[];
  terminalConnected?: boolean;
  terminalStatus?: string;
  metrics?: NodeMetrics;
  capacity?: ResourceValues;
};

export type DashboardService = {
  name?: string;
  fullName?: string;
  stack?: string;
  region?: string;
  node?: string;
  exposure?: string;
  domain?: string;
  targetPort?: string;
  routeId?: string;
  network?: string;
  image?: string;
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

export type DashboardPayload = {
  cluster?: {
    id?: string;
    version?: string;
  };
  readiness?: Readiness;
  nodes?: DashboardNode[];
  services?: DashboardService[];
  trafficPaths?: TrafficPath[];
  storage?: {
    storageClasses?: DashboardStorageClass[];
    volumes?: DashboardVolume[];
    warnings?: string[];
  };
  issues?: DashboardIssue[];
  errors?: string[];
};

export type SyncStatus = "notConnected" | "refreshing" | "updated" | "unavailable" | "tokenRejected";

export type MetricPoint = [number, number];

export type MetricSeries = Record<string, MetricPoint[]>;

export type MetricsHistoryPayload = {
  kind?: "node" | "service";
  name?: string;
  window?: number;
  series?: MetricSeries;
  updatedAt?: number;
};
