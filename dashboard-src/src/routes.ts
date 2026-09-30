import type { NavPage } from "./dashboardViewModel";

// Canonical route path for each top-level nav page. Navigation writes these; the
// router resolves the current path back to a page via `pageForPath`.
export const ROUTE_BY_PAGE: Record<NavPage, string> = {
  overview: "/",
  applications: "/apps",
  deploy: "/create",
  builder: "/builds",
  deployments: "/deployments",
  nodes: "/fleet",
  setup: "/setup",
  observability: "/observe",
  storage: "/storage",
  registry: "/registry",
  credentials: "/settings/secrets",
  maintenance: "/settings/maintenance",
};

// Destinations that moved when the navigation was regrouped. Bookmarks and links in
// release notes keep working; the router replaces them before any page renders.
const LEGACY_ROUTES: [string, string][] = [
  ["/fleet/maintenance", "/settings/maintenance"],
  ["/settings/storage", "/storage"],
];

export function legacyRedirect(path: string): string | null {
  for (const [from, to] of LEGACY_ROUTES) {
    if (path === from || path.startsWith(`${from}/`)) return to;
  }
  return null;
}

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
  if (path === "/fleet" || path.startsWith("/fleet/")) return "nodes";
  if (path === "/setup" || path.startsWith("/setup/")) return "setup";
  if (path === "/observe" || path.startsWith("/observe/")) return "observability";
  if (path === "/storage" || path.startsWith("/storage/")) return "storage";
  if (path === "/registry" || path.startsWith("/registry/")) return "registry";
  if (path === "/settings/maintenance" || path.startsWith("/settings/maintenance/")) return "maintenance";
  if (path === "/settings" || path.startsWith("/settings/")) return "credentials";
  return "notfound";
}
