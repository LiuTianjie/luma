import { dump, load } from "js-yaml";
import type { ComposeDeploymentDraft, ComposeServiceDraft, ComposeVolumeDraft, KeyValueRow, ServiceManifestDraft } from "./types";

type YamlMap = Record<string, unknown>;

function linesFromList(values: string): string[] {
  return values.split("\n").map((item) => item.trim()).filter(Boolean);
}

function validRows(rows: KeyValueRow[]): KeyValueRow[] {
  return rows.filter((row) => row.key.trim());
}

function envValue(row: KeyValueRow): string {
  const value = row.value.trim();
  if (row.kind !== "secret") return value;
  return value;
}

function dumpYaml(value: YamlMap): string {
  return dump(value, {
    lineWidth: -1,
    noRefs: true,
    sortKeys: false,
  });
}

function yamlMap(value: unknown): YamlMap {
  return value && typeof value === "object" && !Array.isArray(value) ? value as YamlMap : {};
}

function loadYamlMap(text: string): YamlMap {
  return yamlMap(load(text) || {});
}

function envMap(rows: KeyValueRow[]): Record<string, string> {
  const result: Record<string, string> = {};
  for (const row of validRows(rows)) result[row.key.trim()] = envValue(row);
  return result;
}

function rawStorageMap(text: string): YamlMap {
  if (!text.trim()) return {};
  return loadYamlMap(text);
}

export function serviceDraftToYaml(draft: ServiceManifestDraft): string {
  const manifest: YamlMap = {
    name: draft.name,
    image: draft.image,
    region: draft.region,
  };
  if (draft.node) manifest.node = draft.node;
  manifest.exposure = draft.exposure;
  if (draft.exposure !== "none") {
    manifest.domain = draft.domain;
    manifest.port = Number(draft.port || 0);
  }
  if (draft.publishPort) manifest.publishPort = Number(draft.publishPort);
  manifest.replicas = draft.replicas;
  if (draft.proxy) manifest.proxy = true;
  const environment = envMap(draft.env);
  if (Object.keys(environment).length) manifest.env = environment;
  if (draft.command.trim()) manifest.command = draft.command.trim();
  const labels = linesFromList(draft.labels);
  if (labels.length) manifest.labels = labels;
  const networks = linesFromList(draft.networks);
  if (networks.length) manifest.networks = networks;
  const structuredVolumes = (draft.volumeMounts || [])
    .filter((volume) => volume.name.trim() && volume.target.trim())
    .map((volume) => `${volume.name.trim()}:${volume.target.trim()}`);
  const volumes = [...structuredVolumes, ...linesFromList(draft.volumes)];
  if (volumes.length) manifest.volumes = volumes;
  const storageVolumes = (draft.volumeMounts || [])
    .filter((volume) => volume.storageMode === "storageClass" && volume.name.trim() && volume.storageClass.trim());
  const storage: YamlMap = rawStorageMap(draft.storage);
  if (storageVolumes.length) {
    for (const volume of storageVolumes) {
      storage[volume.name.trim()] = {
        storageClass: volume.storageClass.trim(),
        path: volume.path.trim() || `${draft.name}/${volume.name.trim()}`,
      };
    }
  }
  if (Object.keys(storage).length) manifest.storage = storage;
  if (draft.cpuLimit || draft.memoryLimit) {
    manifest.resources = {
      limits: {
        ...(draft.cpuLimit ? { cpus: draft.cpuLimit } : {}),
        ...(draft.memoryLimit ? { memory: draft.memoryLimit } : {}),
      },
    };
  }
  if (draft.healthcheckUrl.trim()) {
    manifest.healthcheck = {
      test: ["CMD-SHELL", `wget -qO- ${draft.healthcheckUrl.trim()} || exit 1`],
      interval: "30s",
      timeout: "5s",
      retries: 3,
    };
  }
  return dumpYaml(manifest);
}

export function syncComposeYamlWithDraft(draft: ComposeDeploymentDraft): ComposeDeploymentDraft {
  if (!draft.dockerComposeYaml.trim()) return draft;
  try {
    const compose = loadYamlMap(draft.dockerComposeYaml);
    const services = yamlMap(compose.services);
    for (const service of draft.services) {
      const serviceBody = yamlMap(services[service.name]);
      if (!Object.keys(serviceBody).length) continue;
      const environment = envMap(service.env || []);
      if (Object.keys(environment).length) serviceBody.environment = environment;
      else delete serviceBody.environment;
      services[service.name] = serviceBody;
    }
    compose.services = services;
    return { ...draft, dockerComposeYaml: dumpYaml(compose) };
  } catch {
    return draft;
  }
}

export function composeDraftToSidecarYaml(draft: ComposeDeploymentDraft): string {
  const sidecar: YamlMap = {
    name: draft.name,
    compose: draft.composeFileName || "docker-compose.yml",
    region: draft.region,
  };
  const configuredVolumes = draft.volumes.filter((volume) => volume.storageClass || volume.localNode || volume.localPath);
  if (configuredVolumes.length) {
    const volumes: YamlMap = {};
    for (const volume of configuredVolumes) {
      if (volume.storageMode === "storageClass") {
        volumes[volume.name] = {
          storageClass: volume.storageClass,
          path: `${draft.name}/${volume.name}`,
        };
      } else if (volume.storageMode === "local") {
        volumes[volume.name] = {
          local: {
            node: volume.localNode,
            path: volume.localPath,
          },
        };
      }
    }
    sidecar.volumes = volumes;
  }
  if (draft.services.length) {
    const services: YamlMap = {};
    for (const service of draft.services) {
      const item: YamlMap = { exposure: service.exposure };
      if (service.region) item.region = service.region;
      if (service.node) item.node = service.node;
      if (service.exposure !== "none") {
        item.domain = service.domain;
        item.port = Number(service.port || 0);
      }
      if (service.publishPort) item.publishPort = Number(service.publishPort);
      item.replicas = service.replicas;
      if (service.proxy) item.proxy = true;
      services[service.name] = item;
    }
    sidecar.services = services;
  }
  return dumpYaml(sidecar);
}

export function composeServiceNamesFromYaml(yaml: string): string[] {
  try {
    const services = yamlMap(loadYamlMap(yaml).services);
    return Object.keys(services);
  } catch {
    return [];
  }
}

export function composeVolumeNamesFromYaml(yaml: string): string[] {
  try {
    const volumes = yamlMap(loadYamlMap(yaml).volumes);
    return Object.keys(volumes);
  } catch {
    return [];
  }
}

export function updateComposeServiceExposure(service: ComposeServiceDraft, exposure: ComposeServiceDraft["exposure"]): ComposeServiceDraft {
  if (exposure === "cn-edge") return { ...service, exposure, region: "cn" };
  if (exposure === "external-edge") return { ...service, exposure, region: "global" };
  if (exposure === "tailscale-relay") return { ...service, exposure, region: "home" };
  return { ...service, exposure };
}

export function serviceExposureRegion(exposure: ServiceManifestDraft["exposure"], current: ServiceManifestDraft["region"]): ServiceManifestDraft["region"] {
  if (exposure === "cn-edge") return "cn";
  if (exposure === "external-edge") return "global";
  if (exposure === "tailscale-relay") return "home";
  return current;
}
