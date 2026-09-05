import type { DashboardService } from "../types";

export type ApplicationEndpoint = { address: string; protocol: "http" | "tcp"; href?: string };

// Exposure is authoritative: database relays must never become browser HTTPS links.
export function applicationEndpoints(services: DashboardService[]): ApplicationEndpoint[] {
  const endpoints = new Map<string, ApplicationEndpoint>();
  for (const service of services) {
    const domain = (service.domain || "").trim();
    if (!domain || service.exposure === "none") continue;
    let endpoint: ApplicationEndpoint;
    if (service.exposure === "tcp-relay") {
      const host = domain.replace(/^(?:tcp|https?):\/\//i, "").replace(/\/$/, "");
      const port = service.publishPort || service.targetPort;
      const hasPort = /^\[[^\]]+\]:\d+$/.test(host) || /^[^:]+:\d+$/.test(host);
      const formattedHost = host.includes(":") && !host.startsWith("[") && !hasPort ? `[${host}]` : host;
      const address = port && !hasPort ? `${formattedHost}:${port}` : formattedHost;
      endpoint = { address, protocol: "tcp" };
    } else {
      if (domain.includes("://") && !/^https?:\/\//i.test(domain)) continue;
      const candidate = /^https?:\/\//i.test(domain) ? domain : `https://${domain}`;
      try {
        const url = new URL(candidate);
        if (!url.hostname || url.username || url.password || !["http:", "https:"].includes(url.protocol)) continue;
        endpoint = { address: domain, protocol: "http", href: url.href };
      } catch {
        continue;
      }
    }
    endpoints.set(`${endpoint.protocol}:${endpoint.address}`, endpoint);
  }
  return [...endpoints.values()];
}
