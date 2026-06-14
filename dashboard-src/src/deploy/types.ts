import type { DashboardNode, DashboardStorageClass } from "../types";

export type DeployMode = "service" | "compose";
export type Region = "cn" | "global" | "home";
export type Exposure = "none" | "cn-edge" | "external-edge" | "tailscale-relay" | "cloudflare-tunnel" | "tcp-relay";

export type KeyValueRow = {
  id: string;
  key: string;
  value: string;
  kind?: "plain" | "secret";
};

export type ServiceVolumeDraft = {
  id: string;
  name: string;
  target: string;
  storageMode: "unmanaged" | "storageClass";
  storageClass: string;
  path: string;
};

export type ServiceManifestDraft = {
  name: string;
  image: string;
  region: Region;
  node: string;
  exposure: Exposure;
  domain: string;
  port: string;
  publishPort: string;
  replicas: number;
  proxy: boolean;
  env: KeyValueRow[];
  command: string;
  volumeMounts: ServiceVolumeDraft[];
  labels: string;
  networks: string;
  volumes: string;
  storage: string;
  cpuLimit: string;
  memoryLimit: string;
  healthcheckUrl: string;
  skipDns: boolean;
  skipOrchestrator: boolean;
};

export type ComposeServiceDraft = {
  name: string;
  region: Region | "";
  node: string;
  exposure: Exposure;
  domain: string;
  port: string;
  publishPort: string;
  replicas: number;
  proxy: boolean;
  env: KeyValueRow[];
};

export type ComposeVolumeDraft = {
  name: string;
  target: string;
  storageMode: "unmanaged" | "storageClass" | "local";
  storageClass: string;
  localNode: string;
  localPath: string;
};

export type ComposeDeploymentDraft = {
  name: string;
  composeFileName: string;
  region: Region;
  services: ComposeServiceDraft[];
  volumes: ComposeVolumeDraft[];
  dockerComposeYaml: string;
  skipDns: boolean;
  skipOrchestrator: boolean;
};

export type DeployTemplate = {
  id: string;
  mode: DeployMode;
  name: string;
  nameEn?: string;
  description: string;
  descriptionEn?: string;
  tags: string[];
  service?: ServiceManifestDraft;
  compose?: ComposeDeploymentDraft;
};

export type DeployStep = {
  name?: string;
  status?: "start" | "ok" | "fail" | "done" | string;
  message?: string;
  result?: unknown;
};

export type DeployPreviewArtifact = {
  kind: string;
  path: string;
  content: string;
};

export type DeployPreviewResult = {
  service?: string;
  deployment?: string;
  summary?: Record<string, unknown>;
  artifacts?: DeployPreviewArtifact[];
  storage?: {
    storageClasses?: DashboardStorageClass[];
    volumes?: unknown[];
    warnings?: string[];
  };
  warnings?: string[];
};

export type DeployWorkspaceData = {
  nodes: DashboardNode[];
  storageClasses: DashboardStorageClass[];
};
