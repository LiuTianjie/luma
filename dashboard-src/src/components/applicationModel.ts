import type { DashboardService } from "../types";
import { serviceDraft } from "../deploy/templates";
import type { ComposeDeploymentDraft, ComposeServiceDraft, ServiceManifestDraft } from "../deploy/types";

export type Application = {
  stack: string;
  services: DashboardService[];
  domains: string[];
  status: string;
  running: number;
  desired: number;
  exposure: string;
  regions: string[];
};

const SYSTEM_STACKS = new Set(["traefik", "egress", "luma-control"]);

function isSystemService(service: DashboardService) {
  const stack = service.stack || service.name || "";
  return SYSTEM_STACKS.has(stack) || stack.startsWith("luma-storage") || service.name === "cloudflared";
}

export function serviceRuntimeStatus(service: DashboardService) {
  return (service.status || service.health || "").toLowerCase();
}

export function isServiceHealthy(service: DashboardService) {
  const status = serviceRuntimeStatus(service);
  if ((service.failed || 0) > 0 || ["failed", "dead", "lost", "error"].includes(status)) return false;
  if ((service.pending || 0) > 0) return false;
  if ((service.desired || 0) > 0) return (service.running || 0) >= (service.desired || 0);
  return status === "running" || status === "healthy" || status === "complete";
}

function applicationStatus(services: DashboardService[]) {
  if (services.some((service) => (service.failed || 0) > 0 || ["failed", "dead", "lost", "error"].includes(serviceRuntimeStatus(service)))) return "failed";
  if (services.some((service) => (service.pending || 0) > 0)) return "pending";
  if (services.every(isServiceHealthy)) return "running";
  return "degraded";
}

export function groupApplications(services: DashboardService[]): Application[] {
  const groups = new Map<string, DashboardService[]>();
  for (const service of services) {
    if (isSystemService(service)) continue;
    const stack = service.stack || service.name || "";
    if (!stack) continue;
    groups.set(stack, [...(groups.get(stack) || []), service]);
  }
  return [...groups.entries()].map(([stack, items]) => {
    const domains = [...new Set(items.map((service) => service.domain || "").filter(Boolean))];
    const running = items.reduce((sum, service) => sum + (service.running || 0), 0);
    const desired = items.reduce((sum, service) => sum + (service.desired || 0), 0);
    const exposures = [...new Set(items.map((service) => service.exposure || "none"))];
    const regions = [...new Set(items.map((service) => service.region || "-"))];
    return {
      stack,
      services: items,
      domains,
      running,
      desired,
      exposure: exposures.length === 1 ? exposures[0] : "mixed",
      regions,
      status: applicationStatus(items),
    };
  }).sort((a, b) => a.stack.localeCompare(b.stack));
}

export function serviceToDraft(app: Application): ServiceManifestDraft {
  const primary = app.services[0] || {};
  return serviceDraft({
    name: app.stack,
    image: primary.image || "",
    region: (primary.region as ServiceManifestDraft["region"]) || "cn",
    node: "",
    exposure: (primary.exposure as ServiceManifestDraft["exposure"]) || "none",
    domain: primary.domain || "",
    port: primary.targetPort || "",
    replicas: primary.desired || 1,
    proxy: false,
  });
}

export function appToComposeDraft(app: Application): ComposeDeploymentDraft {
  const composeServices: ComposeServiceDraft[] = app.services.map((service) => ({
    name: service.name || "app",
    region: (service.region as ComposeServiceDraft["region"]) || "",
    node: "",
    exposure: (service.exposure as ComposeServiceDraft["exposure"]) || "none",
    domain: service.domain || "",
    port: service.targetPort || "",
    publishPort: "",
    replicas: service.desired || 1,
    proxy: false,
    env: [],
  }));
  const volumeNames = new Set<string>();
  for (const service of app.services) {
    for (const volume of service.storage || []) {
      if (volume.name) volumeNames.add(volume.name);
    }
  }
  const volumes = [...volumeNames].map((name) => {
    const source = app.services.flatMap((service) => service.storage || []).find((volume) => volume.name === name);
    return {
      name,
      target: "",
      storageMode: source?.storageClass ? "storageClass" as const : "local" as const,
      storageClass: source?.storageClass || "",
      localNode: source?.node || "",
      localPath: source?.networkPath || "",
    };
  });
  const composeYaml = [
    "services:",
    ...app.services.flatMap((service) => [
      `  ${service.name || "app"}:`,
      `    image: ${service.image || "replace-me:latest"}`,
    ]),
    ...(volumes.length ? ["volumes:", ...volumes.map((volume) => `  ${volume.name}: {}`)] : []),
    "",
  ].join("\n");
  return {
    name: app.stack,
    composeFileName: "docker-compose.yml",
    region: (app.regions[0] as ComposeDeploymentDraft["region"]) || "cn",
    services: composeServices,
    volumes,
    dockerComposeYaml: composeYaml,
    skipDns: false,
    skipOrchestrator: false,
  };
}
