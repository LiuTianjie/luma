import type { ResolvedPage } from "./routes";

export type DashboardScope = "full" | "overview" | "applications" | "application" | "nodes" | "setup" | "deploy" | "directory" | "fleet" | "network" | "storage" | "metrics" | "none";

export function dashboardScopeForPage(page: ResolvedPage, path = ""): DashboardScope {
  switch (page) {
    case "deployments": case "registry": case "credentials": case "lae": case "notfound": return "none";
    case "overview": return "overview";
    case "applications":
      if (/^\/apps\/[^/]+/.test(path)) return "application";
      // Legacy service identities need full service lookup to resolve their stack.
      return path.startsWith("/services/") || path.startsWith("/terminal/service/") ? "full" : "applications";
    case "nodes":
      if (/^\/fleet\/network(?:\/|$)/.test(path)) return "network";
      if (/^\/fleet\/nodes\/[^/]+/.test(path)) return "full";
      return /^\/fleet\/(join|regions|maintenance)(\/|$)/.test(path) || path.startsWith("/terminal/node/") ? "nodes" : "fleet";
    case "builder": return "nodes";
    case "setup": return "setup";
    case "deploy": return !path || path === "/create" ? "none" : "deploy";
    case "storage": return path.startsWith("/storage/governance") ? "none" : "storage";
    case "observability": return path.startsWith("/observe/metrics") ? "metrics" : "directory";
    default: return "full";
  }
}

export function dashboardQueryForRoute(scope: DashboardScope, path: string, search: string): string {
  const query = new URLSearchParams();
  if (scope === "application") {
    try { query.set("app", decodeURIComponent(path.split("/")[2] || "")); }
    catch { query.set("app", ""); }
  }
  if (scope === "applications") {
    const params = new URLSearchParams(search);
    for (const key of ["q", "status", "region", "offset", "limit"]) {
      const value = params.get(key);
      if (value) query.set(key, value);
    }
  }
  return query.toString();
}
