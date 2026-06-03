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
};

export type DashboardService = {
  name?: string;
  fullName?: string;
  stack?: string;
  region?: string;
  exposure?: string;
  image?: string;
  running?: number;
  desired?: number;
  pending?: number;
  failed?: number;
  health?: string;
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
  errors?: string[];
};

export type SyncStatus = "notConnected" | "refreshing" | "updated" | "unavailable" | "tokenRejected";
