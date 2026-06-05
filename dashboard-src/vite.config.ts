import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const devDashboardPayload = {
  cluster: { id: "luma-266ba124", leader: "cn-edge", updatedAt: new Date().toISOString() },
  summary: {
    totalNodes: 4,
    readyNodes: 4,
    totalServices: 8,
    readyServices: 8,
    degradedServices: 0,
    failedServices: 0,
  },
  readiness: {
    dns: { ready: true, provider: "Cloudflare", zone: "itool.tech", target: "8.130.148.30" },
    portainer: { ready: true, apiConfigured: true, endpointConfigured: true },
    swarm: { available: true },
  },
  nodes: [
    { name: "cn-edge", displayName: "cn-edge", region: "cn", role: "manager", state: "ready", availability: "active", leader: true, metrics: { cpuPercent: 21.4, load1: 0.82, memoryUsedPercent: 58.2, memoryTotalBytes: 17179869184 }, capacity: { cpus: 4, memoryBytes: 17179869184 } },
    { name: "home-mac-mini", displayName: "home-mac-mini", region: "home", role: "worker", state: "ready", availability: "active", leader: false, metrics: { cpuPercent: 13.8, load1: 1.1, memoryUsedPercent: 61.5, memoryTotalBytes: 34359738368 }, capacity: { cpus: 10, memoryBytes: 34359738368 } },
    { name: "tailscale-relay", displayName: "tailscale-relay", region: "home", role: "worker", state: "ready", availability: "active", leader: false, metrics: { cpuPercent: 8.1, load1: 0.2, memoryUsedPercent: 44.0, memoryTotalBytes: 8589934592 }, capacity: { cpus: 4, memoryBytes: 8589934592 } },
    { name: "m4mini", displayName: "m4mini", region: "home", role: "worker", state: "ready", availability: "active", leader: false, metrics: { cpuPercent: 29.7, load1: 2.4, memoryUsedPercent: 67.9, memoryTotalBytes: 17179869184 }, capacity: { cpus: 8, memoryBytes: 17179869184 } },
  ],
  services: [
    {
      name: "codex-gitea",
      fullName: "codex-gitea_codex-gitea",
      stack: "codex-gitea",
      region: "home",
      exposure: "tailscale-relay",
      image: "ghcr.io/liutianjie/codex-gitea@sha256:ade6c61734a1b7d53b342356c82251afe9fba93d3a2d7509510320c86652834e",
      desired: 1,
      running: 1,
      pending: 0,
      failed: 0,
      health: "running",
      nodes: ["tailscale-relay"],
    },
    {
      name: "mihomo",
      fullName: "egress_mihomo",
      stack: "egress",
      region: "home",
      exposure: "internal",
      image: "docker.1panel.live/metacubex/mihomo:latest",
      desired: 1,
      running: 1,
      pending: 0,
      failed: 0,
      health: "running",
      nodes: ["m4mini"],
    },
    {
      name: "linkshell-gateway",
      fullName: "linkshell-gateway_linkshell-gateway",
      stack: "linkshell-gateway",
      region: "cn",
      exposure: "cn-edge",
      image: "nickname4th/linkshell-gateway@sha256:a0fdd4f49fd5a9ee4e8990b5b403e32cb75fe883d59477ae3397edc598a04ea2",
      desired: 1,
      running: 1,
      pending: 0,
      failed: 0,
      health: "running",
      nodes: ["cn-edge"],
    },
    {
      name: "luma-control",
      fullName: "luma-control_luma-control",
      stack: "luma-control",
      region: "cn",
      exposure: "cn-edge",
      image: "ghcr.io/liutianjie/luma-control:latest",
      desired: 1,
      running: 1,
      pending: 0,
      failed: 0,
      health: "running",
      nodes: ["cn-edge"],
    },
    {
      name: "agent",
      fullName: "portainer_agent",
      stack: "portainer",
      region: "home",
      exposure: "internal",
      image: "docker.m.daocloud.io/portainer/agent:2.21.5",
      desired: 1,
      running: 1,
      pending: 0,
      failed: 0,
      health: "running",
      nodes: ["home-mac-mini"],
    },
    {
      name: "portainer",
      fullName: "portainer_portainer",
      stack: "portainer",
      region: "home",
      exposure: "internal",
      image: "portainer/portainer-ce:2.21.5",
      desired: 1,
      running: 1,
      pending: 0,
      failed: 0,
      health: "running",
      nodes: ["home-mac-mini"],
    },
    {
      name: "tifenxia-docs",
      fullName: "docs_tifenxia-docs",
      stack: "docs",
      region: "home",
      exposure: "tailscale-relay",
      image: "registry.itool.tech/docs/tifenxia-docs:latest",
      desired: 1,
      running: 1,
      pending: 0,
      failed: 0,
      health: "running",
      nodes: ["tailscale-relay"],
    },
    {
      name: "traefik",
      fullName: "traefik_traefik",
      stack: "traefik",
      region: "cn",
      exposure: "internal",
      image: "traefik:v3",
      desired: 1,
      running: 1,
      pending: 0,
      failed: 0,
      health: "running",
      nodes: ["cn-edge"],
    },
  ],
  trafficPaths: [
    { id: "linkshell-gateway", kind: "cn-edge", domain: "gateway.itool.tech", segments: ["Cloudflare DNS", "8.130.148.30", "Traefik", "linkshell-gateway:8787", "cn-edge"] },
    { id: "luma-control", kind: "cn-edge", domain: "luma.itool.tech", segments: ["Cloudflare DNS", "8.130.148.30", "Traefik", "luma-control:8080", "cn-edge"] },
    { id: "codex-gitea", kind: "tailscale-relay", domain: "codex-bot.itool.tech", segments: ["Cloudflare DNS", "8.130.148.30", "Traefik", "Tailscale", "http://100.115.5.84:8080"] },
    { id: "tifenxia-docs", kind: "tailscale-relay", domain: "tifenxia-docs.itool.tech", segments: ["Cloudflare DNS", "8.130.148.30", "Traefik", "Tailscale", "http://100.115.5.84:18080"] },
    { id: "egress", kind: "internal", domain: "", segments: ["client/internal", "mihomo", "m4mini"] },
    { id: "portainer", kind: "internal", domain: "", segments: ["client/internal", "portainer", "home-mac-mini"] },
  ],
  storage: {
    storageClasses: [
      { name: "home-nfs", provider: "nfs", mode: "external", endpoint: "nas:/srv/luma", regions: ["home"] },
      { name: "cn-nfs", provider: "nfs", mode: "managed", node: "cn-edge", path: "/srv/luma", regions: ["cn"] },
    ],
    volumes: [
      { name: "portainer-data", kind: "bind", storageClass: "local", node: "home-mac-mini", services: ["portainer"] },
      { name: "gitea-data", kind: "bind", storageClass: "local", node: "tailscale-relay", services: ["codex-gitea"] },
    ],
    warnings: [],
  },
  issues: [
    { severity: "warning", kind: "service-pending", target: "egress_mihomo", message: "Service egress_mihomo has 1 pending task" },
    { severity: "warning", kind: "node-memory", target: "m4mini", message: "Node m4mini memory is 67.9%" },
  ],
  errors: [],
};

(devDashboardPayload.services as any[]).forEach((service) => {
  service.resources = {
    reservations: { cpus: 0.25, memoryBytes: 134217728 },
    limits: { cpus: 1, memoryBytes: 536870912 },
    actual: {
      containers: 1,
      cpuPercent: service.name === "mihomo" ? 12.7 : 3.4,
      memoryUsageBytes: service.name === "mihomo" ? 241172480 : 89391104,
      memoryLimitBytes: 536870912,
      memoryPercent: service.name === "mihomo" ? 44.9 : 16.7,
      nodes: service.nodes || [],
    },
  };
  service.tasks = (service.nodes || []).map((node: string, index: number) => ({
    id: `${service.fullName}-${index}`,
    node,
    state: "running",
    desiredState: "running",
    containerId: `${service.fullName}-${index}`.slice(0, 12),
    cpuPercent: service.name === "mihomo" ? 12.7 : 3.4,
    memoryUsageBytes: service.name === "mihomo" ? 241172480 : 89391104,
    memoryPercent: service.name === "mihomo" ? 44.9 : 16.7,
  }));
});

export default defineConfig({
  base: "/dashboard/",
  root: __dirname,
  publicDir: false,
  plugins: [
    react(),
    {
      name: "strip-dashboard-trailing-whitespace",
      generateBundle(_options, bundle) {
        for (const item of Object.values(bundle)) {
          if (item.type === "chunk") {
            item.code = item.code.replace(/[ \t]+$/gm, "");
          } else if (typeof item.source === "string") {
            item.source = item.source.replace(/[ \t]+$/gm, "");
          }
        }
      },
    },
    {
      name: "dev-dashboard-api",
      configureServer(server) {
        const readBody = (request: any) => new Promise<Record<string, any>>((resolve) => {
          let raw = "";
          request.on("data", (chunk: unknown) => {
            raw += chunk.toString("utf-8");
          });
          request.on("end", () => {
            try {
              resolve(raw ? JSON.parse(raw) : {});
            } catch {
              resolve({});
            }
          });
        });
        server.middlewares.use("/v1/dashboard/logs", (request, response) => {
          if (request.method !== "GET") {
            response.statusCode = 405;
            response.end(JSON.stringify({ error: "method not allowed" }));
            return;
          }
          const auth = request.headers.authorization || "";
          if (!auth.startsWith("Bearer ")) {
            response.statusCode = 401;
            response.setHeader("Content-Type", "application/json; charset=utf-8");
            response.end(JSON.stringify({ error: "unauthorized" }));
            return;
          }
          const parsed = new URL(request.url || "/v1/dashboard/logs", "http://localhost");
          const service = parsed.searchParams.get("service") || "luma-control_luma-control";
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.setHeader("Cache-Control", "no-store");
          response.end(JSON.stringify({
            service,
            tail: 160,
            updatedAt: Math.floor(Date.now() / 1000),
            logs: [
              `${new Date().toISOString()} ${service} received health probe`,
              `${new Date().toISOString()} ${service} task heartbeat ok`,
              `${new Date().toISOString()} ${service} route check passed`,
            ],
          }));
        });
        server.middlewares.use("/v1/dashboard", (request, response) => {
          if (request.method !== "GET") {
            response.statusCode = 405;
            response.end(JSON.stringify({ error: "method not allowed" }));
            return;
          }
          const auth = request.headers.authorization || "";
          if (!auth.startsWith("Bearer ")) {
            response.statusCode = 401;
            response.setHeader("Content-Type", "application/json; charset=utf-8");
            response.end(JSON.stringify({ error: "unauthorized" }));
            return;
          }
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.setHeader("Cache-Control", "no-store");
          response.end(JSON.stringify({ ...devDashboardPayload, cluster: { ...devDashboardPayload.cluster, updatedAt: new Date().toISOString() } }));
        });
        server.middlewares.use((request, response, next) => {
          const match = /^\/v1\/deployments\/([^/?]+)\/config(?:\?|$)/.exec(request.url || "");
          if (!match) {
            next();
            return;
          }
          if (request.method !== "GET") {
            response.statusCode = 405;
            response.end(JSON.stringify({ error: "method not allowed" }));
            return;
          }
          const name = decodeURIComponent(match[1]);
          const service = devDashboardPayload.services.find((item) => item.stack === name);
          if (!service) {
            response.statusCode = 404;
            response.setHeader("Content-Type", "application/json; charset=utf-8");
            response.end(JSON.stringify({ error: `deployment not found: ${name}` }));
            return;
          }
          const trafficPath = devDashboardPayload.trafficPaths.find((item) => item.id === service.stack);
          const exposure = service.exposure === "internal" ? "none" : service.exposure;
          const lines = [
            `name: ${service.stack}`,
            `image: ${service.image}`,
            `region: ${service.region || "home"}`,
            `exposure: ${exposure}`,
            `replicas: ${service.desired || 1}`,
          ];
          if (trafficPath?.domain) lines.push(`domain: ${trafficPath.domain}`);
          if (trafficPath?.segments?.length) {
            const target = trafficPath.segments.find((segment) => /:\d+$/.test(segment));
            const port = target?.match(/:(\d+)$/)?.[1];
            if (port) lines.push(`port: ${port}`);
          }
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.setHeader("Cache-Control", "no-store");
          response.end(JSON.stringify({
            kind: "service",
            name: service.stack,
            slug: service.stack,
            sourceName: "console:service.yaml",
            updatedAt: Math.floor(Date.now() / 1000),
            manifest: `${lines.join("\n")}\n`,
            composeContent: "",
          }));
        });
        server.middlewares.use("/v1/deployments/preview", async (request, response) => {
          if (request.method !== "POST") {
            response.statusCode = 405;
            response.end(JSON.stringify({ error: "method not allowed" }));
            return;
          }
          const body = await readBody(request);
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.end(JSON.stringify({
            service: "preview-service",
            summary: { name: "preview-service" },
            artifacts: [{ kind: "stack", path: "stacks/cn/preview-service/stack.yml", content: body.manifest || "" }],
            warnings: [],
          }));
        });
        server.middlewares.use("/v1/compose-deployments/preview", async (request, response) => {
          if (request.method !== "POST") {
            response.statusCode = 405;
            response.end(JSON.stringify({ error: "method not allowed" }));
            return;
          }
          const body = await readBody(request);
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.end(JSON.stringify({
            deployment: "preview-compose",
            summary: { name: "preview-compose" },
            artifacts: [{ kind: "stack", path: "stacks/compose/preview-compose/stack.yml", content: body.composeContent || "" }],
            storage: { storageClasses: devDashboardPayload.storage.storageClasses, volumes: [], warnings: [] },
            warnings: [],
          }));
        });
        server.middlewares.use("/v1/deployments/stream", async (_request, response) => {
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/x-ndjson");
          response.write(JSON.stringify({ status: "start", name: "Render stack", message: "started" }) + "\n");
          response.write(JSON.stringify({ status: "ok", name: "Render stack", message: "Stack rendered" }) + "\n");
          response.write(JSON.stringify({ status: "ok", name: "Deploy Portainer stack", message: "Mock deploy complete" }) + "\n");
          response.end(JSON.stringify({ status: "done", result: { service: "preview-service" } }) + "\n");
        });
        server.middlewares.use("/v1/compose-deployments/stream", async (_request, response) => {
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/x-ndjson");
          response.write(JSON.stringify({ status: "start", name: "Render compose stack", message: "started" }) + "\n");
          response.write(JSON.stringify({ status: "ok", name: "Render compose stack", message: "Compose rendered" }) + "\n");
          response.write(JSON.stringify({ status: "ok", name: "Deploy Portainer stack", message: "Mock compose deploy complete" }) + "\n");
          response.end(JSON.stringify({ status: "done", result: { deployment: "preview-compose" } }) + "\n");
        });
        server.middlewares.use("/v1/applications/restart", async (request, response) => {
          if (request.method !== "POST") {
            response.statusCode = 405;
            response.end(JSON.stringify({ error: "method not allowed" }));
            return;
          }
          const body = await readBody(request);
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.end(JSON.stringify({ stack: body.stack, restarted: [{ name: `${body.stack}_app`, forceUpdate: 1 }] }));
        });
      },
    },
  ],
  build: {
    chunkSizeWarningLimit: 900,
    outDir: "../luma/assets/dashboard",
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        entryFileNames: "app.js",
        chunkFileNames: "app.js",
        assetFileNames: (assetInfo) => {
          if (assetInfo.name?.endsWith(".css")) return "styles.css";
          return "asset-[name][extname]";
        },
      },
    },
  },
});
