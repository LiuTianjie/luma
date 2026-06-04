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
  errors?: string[];
};

export type SyncStatus = "notConnected" | "refreshing" | "updated" | "unavailable" | "tokenRejected";
