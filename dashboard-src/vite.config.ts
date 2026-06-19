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
    nomad: { ready: true, available: true, leader: "100.113.204.125:4647" },
  },
  nodes: [
    { name: "cn-edge", displayName: "cn-edge", region: "cn", role: "manager", state: "ready", availability: "active", leader: true, agentStatus: "ready", agentOs: "linux", storageCapabilities: ["terminal"], terminalConnected: true, terminalStatus: "connected", metrics: { cpuPercent: 21.4, load1: 0.82, memoryUsedPercent: 58.2, memoryTotalBytes: 17179869184 }, capacity: { cpus: 4, memoryBytes: 17179869184 } },
    { name: "home-mac-mini", displayName: "home-mac-mini", region: "home", role: "worker", state: "ready", availability: "active", leader: false, agentStatus: "ready", agentOs: "darwin", storageCapabilities: ["terminal"], terminalConnected: false, terminalStatus: "waiting", metrics: { cpuPercent: 13.8, load1: 1.1, memoryUsedPercent: 61.5, memoryTotalBytes: 34359738368 }, capacity: { cpus: 10, memoryBytes: 34359738368 } },
    { name: "tailscale-relay", displayName: "tailscale-relay", region: "home", role: "worker", state: "ready", availability: "active", leader: false, agentStatus: "ready", agentOs: "linux", storageCapabilities: ["terminal"], terminalConnected: false, terminalStatus: "waiting", metrics: { cpuPercent: 8.1, load1: 0.2, memoryUsedPercent: 44.0, memoryTotalBytes: 8589934592 }, capacity: { cpus: 4, memoryBytes: 8589934592 } },
    { name: "m4mini", displayName: "m4mini", region: "home", role: "worker", state: "ready", availability: "active", leader: false, agentStatus: "ready", agentOs: "darwin", storageCapabilities: ["terminal"], terminalConnected: false, terminalStatus: "waiting", metrics: { cpuPercent: 29.7, load1: 2.4, memoryUsedPercent: 67.9, memoryTotalBytes: 17179869184 }, capacity: { cpus: 8, memoryBytes: 17179869184 } },
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
      name: "mysql",
      fullName: "granary_mysql",
      stack: "granary",
      region: "home",
      exposure: "tcp-relay",
      domain: "granary-db.itool.tech",
      targetPort: 3306,
      publishPort: 3306,
      image: "mysql:8",
      desired: 1,
      running: 1,
      pending: 0,
      failed: 0,
      health: "running",
      nodes: ["tailscale-relay"],
    },
    {
      name: "frontend",
      fullName: "granary_frontend",
      stack: "granary",
      region: "home",
      exposure: "tailscale-relay",
      domain: "granary.itool.tech",
      targetPort: 3000,
      publishPort: 3000,
      image: "ghcr.io/liutianjie/granary-frontend:latest",
      desired: 1,
      running: 1,
      pending: 0,
      failed: 0,
      health: "running",
      nodes: ["tailscale-relay"],
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
    { id: "granary", kind: "tcp-relay", domain: "granary-db.itool.tech", segments: ["Cloudflare DNS", "8.130.148.30:3306", "Traefik TCP", "Tailscale", "100.115.5.84:3306"] },
  ],
  storage: {
    storageClasses: [
      { name: "home-nfs", provider: "nfs", mode: "external", endpoint: "nas:/srv/luma", regions: ["home"] },
      { name: "cn-nfs", provider: "nfs", mode: "managed", node: "cn-edge", path: "/srv/luma", regions: ["cn"] },
    ],
    volumes: [
      { name: "granary-mysql", kind: "volume", storageClass: "local", node: "tailscale-relay", services: ["granary"] },
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

const devNodeAddresses: Record<string, string> = {
  "cn-edge": "100.64.0.1",
  "home-mac-mini": "100.64.0.2",
  "tailscale-relay": "100.115.5.84",
  m4mini: "100.64.0.4",
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
    region: devDashboardPayload.nodes.find((item) => item.name === node)?.region || service.region || "",
    nodeAddress: devNodeAddresses[node] || "",
    state: "running",
    desiredState: "running",
    containerId: `${service.fullName}-${index}`.slice(0, 12),
    cpuPercent: service.name === "mihomo" ? 12.7 : 3.4,
    memoryUsageBytes: service.name === "mihomo" ? 241172480 : 89391104,
    memoryPercent: service.name === "mihomo" ? 44.9 : 16.7,
  }));
});

(devDashboardPayload.trafficPaths as any[]).forEach((path) => {
  const service = (devDashboardPayload.services as any[]).find((item) => item.name === path.id || item.stack === path.id);
  const upstream = [...(path.segments || [])].reverse().find((item: string) => /^https?:\/\//.test(item) || /^\d{1,3}(\.\d{1,3}){3}:/.test(item));
  path.destinations = (service?.tasks || []).map((task: any) => ({
    service: service.fullName || service.name || "",
    region: task.region || service.region || "",
    node: task.node || "",
    nodeAddress: task.nodeAddress || "",
    address: upstream && upstream.includes(task.nodeAddress) ? upstream : "",
    state: task.state || "",
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
        server.middlewares.use("/v1/dashboard/logs/stream", (request, response) => {
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
          const parsed = new URL(request.url || "/v1/dashboard/logs/stream", "http://localhost");
          const service = parsed.searchParams.get("service") || "luma-control_luma-control";
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/x-ndjson");
          response.setHeader("Cache-Control", "no-cache");
          response.write(JSON.stringify({ status: "start", service }) + "\n");
          const samples = ["received health probe", "task heartbeat ok", "route check passed", "GET /healthz 200 1ms", "reconciler tick"];
          let i = 0;
          response.write(JSON.stringify({ line: `${new Date().toISOString()} ${service} stream attached`, ts: Math.floor(Date.now() / 1000) }) + "\n");
          const timer = setInterval(() => {
            const line = `${new Date().toISOString()} ${service} ${samples[i % samples.length]}`;
            i += 1;
            response.write(JSON.stringify({ line, ts: Math.floor(Date.now() / 1000) }) + "\n");
          }, 1200);
          request.on("close", () => clearInterval(timer));
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
        server.middlewares.use("/v1/dashboard/metrics/history", (request, response) => {
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
          const parsed = new URL(request.url || "/v1/dashboard/metrics/history", "http://localhost");
          const kind = parsed.searchParams.get("kind") === "service" ? "service" : "node";
          const name = parsed.searchParams.get("name") || "";
          const windowSeconds = Math.max(60, Number(parsed.searchParams.get("window")) || 3600);
          const step = 30;
          const count = Math.min(720, Math.floor(windowSeconds / step));
          const nowSec = Math.floor(Date.now() / 1000);
          // Stable per-name phase so charts don't reshuffle every poll.
          let seed = 0;
          for (let i = 0; i < name.length; i += 1) seed = (seed * 31 + name.charCodeAt(i)) % 997;
          const wave = (base: number, amp: number, phase: number) =>
            Array.from({ length: count }, (_, i) => {
              const ts = nowSec - (count - 1 - i) * step;
              const v = base + amp * Math.sin((i + phase) / 7) + amp * 0.4 * Math.sin((i + phase) / 2.3);
              return [ts, Math.max(0, Number(v.toFixed(2)))] as [number, number];
            });
          const series =
            kind === "service"
              ? {
                  cpuPercent: wave(18 + (seed % 20), 12, seed),
                  memoryUsageBytes: wave(180_000_000 + (seed % 7) * 20_000_000, 30_000_000, seed + 3),
                }
              : {
                  cpuPercent: wave(24 + (seed % 30), 16, seed),
                  memoryUsedPercent: wave(55 + (seed % 25), 10, seed + 5),
                };
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.setHeader("Cache-Control", "no-store");
          response.end(JSON.stringify({ kind, name, window: windowSeconds, series, updatedAt: nowSec }));
        });
        server.middlewares.use("/v1/secrets", (request, response, next) => {
          if (request.method !== "GET") {
            next();
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
          response.end(JSON.stringify({ secrets: ["CLOUDFLARE_API_TOKEN", "TAILSCALE_AUTHKEY", "granary/DATABASE_URL", "codex-gitea/GITEA_TOKEN"] }));
        });
        server.middlewares.use("/v1/registries", (request, response, next) => {
          if (request.method !== "GET") {
            next();
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
          response.end(JSON.stringify({
            registries: [
              { host: "ghcr.io", serverAddress: "ghcr.io", username: "liutianjie", configured: true },
              { host: "gcode.gaojiua.com:3000", serverAddress: "gcode.gaojiua.com:3000", username: "deploy", configured: true },
            ],
          }));
        });
        server.middlewares.use("/v1/storage", (request, response, next) => {
          if (request.method !== "GET") {
            next();
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
          response.end(JSON.stringify({ storageClasses: devDashboardPayload.storage.storageClasses }));
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
            artifacts: [{ kind: "job", path: "stacks/cn/preview-service/preview-service.nomad.json", content: body.manifest || "" }],
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
            artifacts: [{ kind: "job", path: "stacks/compose/preview-compose/preview-compose.nomad.json", content: body.composeContent || "" }],
            storage: { storageClasses: devDashboardPayload.storage.storageClasses, volumes: [], warnings: [] },
            warnings: [],
          }));
        });
        server.middlewares.use("/v1/deployments/stream", async (_request, response) => {
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/x-ndjson");
          response.write(JSON.stringify({ status: "start", name: "Render Nomad job", message: "started" }) + "\n");
          response.write(JSON.stringify({ status: "ok", name: "Render Nomad job", message: "Nomad job rendered" }) + "\n");
          response.write(JSON.stringify({ status: "ok", name: "Deploy Nomad job", message: "Mock deploy complete" }) + "\n");
          response.end(JSON.stringify({ status: "done", result: { service: "preview-service" } }) + "\n");
        });
        server.middlewares.use("/v1/compose-deployments/stream", async (_request, response) => {
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/x-ndjson");
          response.write(JSON.stringify({ status: "start", name: "Render compose Nomad job", message: "started" }) + "\n");
          response.write(JSON.stringify({ status: "ok", name: "Render compose Nomad job", message: "Compose Nomad job rendered" }) + "\n");
          response.write(JSON.stringify({ status: "ok", name: "Deploy Nomad job", message: "Mock compose deploy complete" }) + "\n");
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
