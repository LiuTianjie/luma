import type { NavPage } from "./dashboardViewModel";

// Canonical route path for each top-level nav page. Navigation writes these; the
// router resolves the current path back to a page via `pageForPath`.
export const ROUTE_BY_PAGE: Record<NavPage, string> = {
  overview: "/",
  applications: "/apps",
  deploy: "/create",
  builder: "/builds",
  deployments: "/deployments",
  lae: "/lae",
  nodes: "/fleet",
  observability: "/observe",
  storage: "/storage",
  registry: "/registry",
  credentials: "/settings/secrets",
};

export type ResolvedPage = NavPage | "notfound";

// Resolve an app-relative path to the page that should render. Prefix-based so nested
// routes (e.g. /apps/:stack, /settings/registries) still resolve to their section.
export function pageForPath(path: string): ResolvedPage {
  if (path === "/") return "overview";
  if (path.startsWith("/terminal/node/")) return "nodes";
  if (path.startsWith("/terminal/service/") || path.startsWith("/services/")) return "applications";
  if (path === "/apps" || path.startsWith("/apps/")) return "applications";
  if (path === "/create" || path.startsWith("/create/")) return "deploy";
  if (path === "/builds" || path.startsWith("/builds/")) return "builder";
  if (path === "/deployments" || path.startsWith("/deployments/")) return "deployments";
  if (path === "/lae" || path.startsWith("/lae/")) return "lae";
  if (path === "/fleet" || path.startsWith("/fleet/")) return "nodes";
  if (path === "/observe" || path.startsWith("/observe/")) return "observability";
  if (path === "/storage" || path.startsWith("/storage/")) return "storage";
  if (path === "/registry" || path.startsWith("/registry/")) return "registry";
  if (path === "/settings" || path.startsWith("/settings/")) return "credentials";
  return "notfound";
}
