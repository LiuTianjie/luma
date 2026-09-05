import { load } from "js-yaml";
import type { DeployMode } from "./types";

export type SubmissionSummary = {
  name: string;
  region: string;
  services: string[];
  images: string[];
  ingress: string[];
  volumes: string[];
};
const map = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (value: unknown): string => typeof value === "string" || typeof value === "number" ? String(value) : "";

/** Read only the exact documents sent to Control; never derive impact from stale form state. */
export function submissionSummary(mode: DeployMode, manifest: string, composeContent = ""): SubmissionSummary {
  const parsed = load(manifest);
  const body = map(parsed);
  if (!Object.keys(body).length) throw new Error("YAML must contain a mapping / YAML 必须为配置对象");
  const name = text(body.name).trim();
  if (!name) throw new Error("YAML name is required / YAML 必须包含 name");
  const configs = mode === "compose" ? map(body.services) : { [name]: body };
  const compose = mode === "compose" ? map(load(composeContent)) : {};
  const services = mode === "compose" ? map(compose.services) : configs;
  if (!Object.keys(services).length) throw new Error("No services in YAML / YAML 中没有服务");
  const images = Object.entries(services).map(([key, value]) => `${key}: ${text(map(value).image) || "—"}`);
  const ingress = Object.entries(configs).filter(([, value]) => text(map(value).exposure) && map(value).exposure !== "none")
    .map(([key, value]) => { const service = map(value); return `${key}: ${text(service.exposure)} · ${text(service.domain) || "—"}:${text(service.port) || "—"}`; });
  const volumeNames = Object.keys(map(mode === "compose" ? body.volumes : body.storage));
  for (const service of Object.values(services)) {
    const mounts = map(service).volumes;
    for (const mount of Array.isArray(mounts) ? mounts : []) {
      const source = typeof mount === "string" ? (mount.includes(":") ? mount.split(":")[0] : "") : text(map(mount).source);
      if (source) volumeNames.push(source);
    }
  }
  return { name, region: text(body.region), services: Object.keys(services), images, ingress, volumes: [...new Set(volumeNames)] };
}
