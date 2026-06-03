import type { ComposeDeploymentDraft, ComposeServiceDraft, ComposeVolumeDraft, KeyValueRow, ServiceManifestDraft } from "./types";

function scalar(value: string | number | boolean): string {
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (!value) return "\"\"";
  if (/^[A-Za-z0-9._/@:+${}-]+$/.test(value)) return value;
  return JSON.stringify(value);
}

function linesFromList(values: string): string[] {
  return values.split("\n").map((item) => item.trim()).filter(Boolean);
}

function validRows(rows: KeyValueRow[]): KeyValueRow[] {
  return rows.filter((row) => row.key.trim());
}

export function serviceDraftToYaml(draft: ServiceManifestDraft): string {
  const lines = [
    `name: ${scalar(draft.name)}`,
    `image: ${scalar(draft.image)}`,
    `region: ${draft.region}`,
  ];
  if (draft.node) lines.push(`node: ${scalar(draft.node)}`);
  lines.push(`exposure: ${draft.exposure}`);
  if (draft.exposure !== "none") {
    lines.push(`domain: ${scalar(draft.domain)}`);
    lines.push(`port: ${Number(draft.port || 0)}`);
  }
  if (draft.publishPort) lines.push(`publishPort: ${Number(draft.publishPort)}`);
  lines.push(`replicas: ${draft.replicas}`);
  if (draft.proxy) lines.push("proxy: true");
  const envRows = validRows(draft.env);
  if (envRows.length) {
    lines.push("env:");
    for (const row of envRows) lines.push(`  ${row.key.trim()}: ${scalar(row.value.trim())}`);
  }
  if (draft.command.trim()) lines.push(`command: ${scalar(draft.command.trim())}`);
  const labels = linesFromList(draft.labels);
  if (labels.length) {
    lines.push("labels:");
    for (const label of labels) lines.push(`  - ${scalar(label)}`);
  }
  const networks = linesFromList(draft.networks);
  if (networks.length) {
    lines.push("networks:");
    for (const network of networks) lines.push(`  - ${scalar(network)}`);
  }
  const volumes = linesFromList(draft.volumes);
  if (volumes.length) {
    lines.push("volumes:");
    for (const volume of volumes) lines.push(`  - ${scalar(volume)}`);
  }
  if (draft.cpuLimit || draft.memoryLimit) {
    lines.push("resources:");
    lines.push("  limits:");
    if (draft.cpuLimit) lines.push(`    cpus: ${scalar(draft.cpuLimit)}`);
    if (draft.memoryLimit) lines.push(`    memory: ${scalar(draft.memoryLimit)}`);
  }
  if (draft.healthcheckUrl.trim()) {
    lines.push("healthcheck:");
    lines.push(`  test: ["CMD-SHELL", "wget -qO- ${draft.healthcheckUrl.trim()} || exit 1"]`);
    lines.push("  interval: 30s");
    lines.push("  timeout: 5s");
    lines.push("  retries: 3");
  }
  return `${lines.join("\n")}\n`;
}

export function composeDraftToSidecarYaml(draft: ComposeDeploymentDraft): string {
  const lines = [
    `name: ${scalar(draft.name)}`,
    `compose: ${scalar(draft.composeFileName || "docker-compose.yml")}`,
    `region: ${draft.region}`,
  ];
  const configuredVolumes = draft.volumes.filter((volume) => volume.storageClass || volume.localNode || volume.localPath);
  if (configuredVolumes.length) {
    lines.push("volumes:");
    for (const volume of configuredVolumes) {
      lines.push(`  ${volume.name}:`);
      if (volume.storageMode === "storageClass") {
        lines.push(`    storageClass: ${scalar(volume.storageClass)}`);
        lines.push(`    path: ${scalar(`${draft.name}/${volume.name}`)}`);
      } else {
        lines.push("    local:");
        lines.push(`      node: ${scalar(volume.localNode)}`);
        lines.push(`      path: ${scalar(volume.localPath)}`);
      }
    }
  }
  if (draft.services.length) {
    lines.push("services:");
    for (const service of draft.services) {
      lines.push(`  ${service.name}:`);
      if (service.region) lines.push(`    region: ${service.region}`);
      if (service.node) lines.push(`    node: ${scalar(service.node)}`);
      lines.push(`    exposure: ${service.exposure}`);
      if (service.exposure !== "none") {
        lines.push(`    domain: ${scalar(service.domain)}`);
        lines.push(`    port: ${Number(service.port || 0)}`);
      }
      if (service.publishPort) lines.push(`    publishPort: ${Number(service.publishPort)}`);
      lines.push(`    replicas: ${service.replicas}`);
      if (service.proxy) lines.push("    proxy: true");
    }
  }
  return `${lines.join("\n")}\n`;
}

export function composeServiceNamesFromYaml(yaml: string): string[] {
  const names: string[] = [];
  const lines = yaml.split("\n");
  let inServices = false;
  for (const line of lines) {
    if (/^services:\s*$/.test(line)) {
      inServices = true;
      continue;
    }
    if (inServices && /^[A-Za-z0-9_-]+:/.test(line)) break;
    const match = inServices ? line.match(/^  ([A-Za-z0-9_.-]+):\s*$/) : null;
    if (match) names.push(match[1]);
  }
  return names;
}

export function composeVolumeNamesFromYaml(yaml: string): string[] {
  const names: string[] = [];
  const lines = yaml.split("\n");
  let inVolumes = false;
  for (const line of lines) {
    if (/^volumes:\s*$/.test(line)) {
      inVolumes = true;
      continue;
    }
    if (inVolumes && /^[A-Za-z0-9_-]+:/.test(line)) break;
    const match = inVolumes ? line.match(/^  ([A-Za-z0-9_.-]+):\s*(?:\{\})?\s*$/) : null;
    if (match) names.push(match[1]);
  }
  return names;
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
