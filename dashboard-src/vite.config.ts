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

const devRegistryPayload = {
  registry: { host: "100.66.177.70:5000", node: "builder", volumeName: "luma-registry-data", jobId: "luma-registry" },
  summary: { repositoryCount: 144, tagCount: 1122, manifestCount: 486, protectedCount: 173, retainedCount: 284, candidateCount: 29, unknownCount: 0, scanErrors: 0, scannedAt: Math.floor(Date.now() / 1000), durationMs: 4380 },
  usage: {
    volumeBytes: 27_742_000_000,
    filesystemTotalBytes: 250_000_000_000,
    filesystemUsedBytes: 162_500_000_000,
    filesystemAvailableBytes: 87_500_000_000,
    filesystemUsePercent: 65,
    monthlyBlobs: [
      { month: "2026-03", bytes: 1_900_000_000, files: 126 },
      { month: "2026-04", bytes: 2_600_000_000, files: 183 },
      { month: "2026-05", bytes: 3_200_000_000, files: 207 },
      { month: "2026-06", bytes: 4_800_000_000, files: 315 },
      { month: "2026-07", bytes: 6_100_000_000, files: 442 },
      { month: "2026-08", bytes: 5_300_000_000, files: 381 },
    ],
  },
  protectionComplete: true,
  referenceError: "",
  policy: { mode: "recommend", keepLast: 20, maxAgeDays: 30, systemKeepLast: 3, queueGraceHours: 0, gcGraceDays: 7, warningPercent: 75, criticalPercent: 85, emergencyPercent: 92 },
  entries: [
    { repository: "lae/agent-controller", digest: `sha256:${"a".repeat(64)}`, tags: ["2026.08.17-8d91ab", "latest"], logicalBytes: 724_000_000, createdAt: 1786900000, platforms: ["linux/amd64", "linux/arm64"], protectionStatus: "protected", protectionReasons: [{ kind: "nomad-version", source: "nomad:lae-agent-controller:v18" }] },
    { repository: "luma-control", digest: `sha256:${"b".repeat(64)}`, tags: ["0.1.281", "latest"], logicalBytes: 381_000_000, createdAt: 1786800000, platforms: ["linux/amd64"], protectionStatus: "protected", protectionReasons: [{ kind: "system", source: "system retention" }] },
    { repository: "tifenxia/api", digest: `sha256:${"c".repeat(64)}`, tags: ["release-20260815"], logicalBytes: 1_840_000_000, createdAt: 1786500000, platforms: ["linux/amd64"], protectionStatus: "protected", protectionReasons: [{ kind: "deployment", source: "services:tifenxia-api" }] },
    { repository: "granary/frontend", digest: `sha256:${"d".repeat(64)}`, tags: ["main-f81d2c"], logicalBytes: 186_000_000, createdAt: 1784300000, platforms: ["linux/amd64"], protectionStatus: "retained", protectionReasons: [] },
    { repository: "granary/frontend", digest: `sha256:${"e".repeat(64)}`, tags: ["main-25a90a"], logicalBytes: 181_000_000, createdAt: 1781200000, platforms: ["linux/amd64"], protectionStatus: "candidate", protectionReasons: [] },
    { repository: "docs/tifenxia-docs", digest: `sha256:${"f".repeat(64)}`, tags: ["preview-418", "preview-latest"], logicalBytes: 93_000_000, createdAt: 1779000000, platforms: ["linux/amd64"], protectionStatus: "candidate", protectionReasons: [] },
  ],
  deletions: [
    { id: "registry-delete-7f13b2c4", status: "deleted_pending_gc", manifests: [{ repository: "sandbox/old-worker", digest: `sha256:${"1".repeat(64)}` }], logicalBytes: 612_000_000, createdAt: 1786600000, updatedAt: 1786686400, gcAfter: 1787291200, message: "2 manifests deleted; blobs retained until GC" },
    { id: "registry-delete-a19c3d28", status: "queued", manifests: [{ repository: "granary/frontend", digest: `sha256:${"e".repeat(64)}` }], logicalBytes: 181_000_000, createdAt: 1786900000, updatedAt: 1786900000, notBefore: 1786986400, message: "Deletion queued" },
  ],
};

export default defineConfig({
  base: "/dashboard/",
  root: __dirname,
  // Opt-in local integration: existing infrastructure fixtures remain mocked,
  // while durable history/alerts/governance run against an isolated Control.
  server: process.env.LUMA_DEV_CONTROL_URL ? {
    proxy: {
      "^/v1/(history|alerting|governance)(/|\\?|$)": { target: process.env.LUMA_DEV_CONTROL_URL, changeOrigin: true },
    },
  } : undefined,
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
        // Dev-only log fixture: bounded streams intentionally close so the
        // dashboard exercises clean-EOF reconnect and cursor recovery.
        const logEpoch = Date.now();
        const logSources = [
          { allocationId: "mock-instance-a", task: "app", stream: "stdout" },
          { allocationId: "mock-instance-b", task: "app", stream: "stderr" },
        ];
        const sampleLogs = (service: string, after: number, allocation: string) => {
          const latest = Math.floor((Date.now() - logEpoch) / 1200) + 3;
          return Array.from({ length: Math.max(0, latest - after) }, (_, index) => {
            const sequence = after + index + 1;
            const source = logSources[sequence % logSources.length];
            return {
              ...source, cursor: String(sequence), observedAt: Math.floor(Date.now() / 1000),
              line: `${service} sample event ${sequence}`,
            };
          }).filter((entry) => !allocation || entry.allocationId === allocation);
        };
        server.middlewares.use("/v1/dashboard/logs/stream", (request, response) => {
          if (request.method !== "GET" || !request.headers.authorization?.startsWith("Bearer ")) {
            response.statusCode = request.method !== "GET" ? 405 : 401;
            response.end(JSON.stringify({ error: "unauthorized or unsupported method" }));
            return;
          }
          const parsed = new URL(request.url || "/v1/dashboard/logs/stream", "http://localhost");
          if (parsed.searchParams.has("since")) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "since is unsupported; use tail and cursor" }));
            return;
          }
          const service = parsed.searchParams.get("service") || "luma-control_luma-control";
          const allocation = parsed.searchParams.get("allocation") || "";
          const tail = Math.min(500, Math.max(1, Number(parsed.searchParams.get("tail") || "200")));
          let cursor = parsed.searchParams.has("cursor") ? Number(parsed.searchParams.get("cursor"))
            : Math.max(0, Math.floor((Date.now() - logEpoch) / 1200) + 3 - tail);
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/x-ndjson");
          response.setHeader("Cache-Control", "no-cache");
          response.write(JSON.stringify({ status: "start", service, sources: logSources, capabilities: { since: false, resume: true }, warnings: [], limits: { maxSources: 32, maxBytesPerSource: 65536, pollIntervalSeconds: 2, tailScope: "total" } }) + "\n");
          let ticks = 0;
          const send = () => {
            for (const entry of sampleLogs(service, cursor, allocation)) {
              cursor = Number(entry.cursor);
              response.write(JSON.stringify(entry) + "\n");
            }
            response.write(JSON.stringify({ status: "heartbeat", cursor: String(cursor) }) + "\n");
          };
          send();
          const timer = setInterval(() => {
            send();
            if (++ticks >= 6) { clearInterval(timer); response.end(); }
          }, 1200);
          response.on("close", () => clearInterval(timer));
        });
        server.middlewares.use("/v1/dashboard/logs", (request, response) => {
          if (request.method !== "GET" || !request.headers.authorization?.startsWith("Bearer ")) {
            response.statusCode = request.method !== "GET" ? 405 : 401;
            response.end(JSON.stringify({ error: "unauthorized or unsupported method" }));
            return;
          }
          const parsed = new URL(request.url || "/v1/dashboard/logs", "http://localhost");
          if (parsed.searchParams.has("since")) {
            response.statusCode = 400;
            response.end(JSON.stringify({ error: "since is unsupported; use tail and cursor" }));
            return;
          }
          const service = parsed.searchParams.get("service") || "luma-control_luma-control";
          const allocation = parsed.searchParams.get("allocation") || "";
          const tail = Math.min(500, Math.max(1, Number(parsed.searchParams.get("tail") || "200")));
          const after = Math.max(0, Math.floor((Date.now() - logEpoch) / 1200) + 3 - tail);
          const entries = sampleLogs(service, after, allocation).slice(-tail);
          response.statusCode = 200;
          response.setHeader("Cache-Control", "no-store");
          if (parsed.searchParams.get("download") === "1") {
            response.setHeader("Content-Type", "text/plain; charset=utf-8");
            response.end(entries.map((entry) => `[${entry.allocationId} / ${entry.task} / ${entry.stream}] ${entry.line}`).join("\n") + "\n");
            return;
          }
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.end(JSON.stringify({
            service, tail, updatedAt: Math.floor(Date.now() / 1000), entries,
            logs: entries.map((entry) => `[${entry.allocationId} / ${entry.task} / ${entry.stream}] ${entry.line}`), sources: logSources,
            cursor: entries.at(-1)?.cursor || "0", warnings: [],
            capabilities: { since: false, resume: true },
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
          const requestedWindow = Number(parsed.searchParams.get("window")) || 3600;
          const windowSeconds = Math.min(21600, Math.max(60, requestedWindow));
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
          response.end(JSON.stringify({
            kind, name, requestedWindow, window: windowSeconds, series, updatedAt: nowSec,
            retentionSeconds: 21600, sampleIntervalSeconds: step,
            availableFrom: nowSec - (count - 1) * step, latestSampleAt: nowSec,
          }));
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
        server.middlewares.use("/v1/registry/inventory", (request, response) => {
          if (request.method !== "GET") {
            response.statusCode = 405;
            response.end(JSON.stringify({ error: "method not allowed" }));
            return;
          }
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.setHeader("Cache-Control", "no-store");
          response.end(JSON.stringify(devRegistryPayload));
        });
        server.middlewares.use("/v1/registry/deletions/preview", async (request, response) => {
          const body = await readBody(request);
          const requested = Array.isArray(body.manifests) ? body.manifests : [];
          const selected = devRegistryPayload.entries.filter((entry) => requested.some((item: any) => item.repository === entry.repository && item.digest === entry.digest));
          const risks = selected.flatMap((entry) => (entry.protectionReasons || []).map((reason) => ({ ...entry, reason: `manifest is referenced by ${reason.source || reason.kind}` })));
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.end(JSON.stringify({ allowed: selected.length > 0, selected, dependentManifests: [], blocked: [], risks, logicalBytes: selected.reduce((total, item) => total + item.logicalBytes, 0) }));
        });
        server.middlewares.use("/v1/registry/purge", async (request, response) => {
          const body = await readBody(request);
          const requested = Array.isArray(body.manifests) ? body.manifests : [];
          const purged = devRegistryPayload.entries.filter((entry) => requested.some((item: any) => item.repository === entry.repository && item.digest === entry.digest));
          devRegistryPayload.entries = devRegistryPayload.entries.filter((entry) => !purged.includes(entry));
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.end(JSON.stringify({
            purged: purged.map((item) => ({ repository: item.repository, digest: item.digest })),
            manifestCount: purged.length,
            reclaimedBytes: purged.reduce((total, item) => total + item.logicalBytes, 0),
            collectedBlobs: purged.length,
            sharedLayersOnly: false,
            risks: [],
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
        server.middlewares.use("/v1/builds", (request, response, next) => {
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
          response.end(JSON.stringify({ runs: [] }));
        });
        server.middlewares.use("/v1/deployments/history", (request, response, next) => {
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
          const nowSec = Math.floor(Date.now() / 1000);
          const devSteps: Record<string, Array<{ name: string; status: string; message?: string }>> = {
            "deploy-1": [
              { name: "Parse manifest", status: "ok", message: "linkshell-gateway -> cn/cn-edge" },
              { name: "Resolve image", status: "ok" },
              { name: "Write route file", status: "ok" },
              { name: "Submit Nomad job", status: "ok" },
              { name: "Probe public route", status: "ok", message: "HTTP 200" },
            ],
            "deploy-2": [
              { name: "Parse sidecar", status: "ok", message: "granary -> home" },
              { name: "Prepare managed storage", status: "ok" },
              { name: "Submit Nomad job", status: "ok" },
            ],
            "deploy-3": [
              { name: "Parse manifest", status: "ok" },
              { name: "Resolve image", status: "ok" },
              { name: "Probe public route", status: "fail", message: "Public route unhealthy: HTTP 404 (Traefik router not found)" },
            ],
          };
          const detailMatch = /^\/([^/?]+)/.exec(request.url || "");
          if (detailMatch) {
            const id = detailMatch[1];
            const steps = devSteps[id];
            response.statusCode = steps ? 200 : 404;
            response.setHeader("Content-Type", "application/json; charset=utf-8");
            response.setHeader("Cache-Control", "no-store");
            response.end(steps
              ? JSON.stringify({ event: { id, steps } })
              : JSON.stringify({ error: `deployment event not found: ${id}` }));
            return;
          }
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.setHeader("Cache-Control", "no-store");
          response.end(JSON.stringify({
            events: [
              { id: "deploy-1", kind: "service", name: "linkshell-gateway", slug: "linkshell-gateway", sourceName: "service.yaml", origin: "cli", status: "active", stepCount: 5, createdAt: nowSec - 120 },
              { id: "deploy-2", kind: "compose", name: "granary", slug: "granary", sourceName: "luma.compose.yml", origin: "dashboard", status: "active", stepCount: 3, createdAt: nowSec - 900 },
              { id: "deploy-3", kind: "service", name: "codex-gitea", slug: "codex-gitea", sourceName: "service.yaml", origin: "cli", status: "failed_partial", stepCount: 3, createdAt: nowSec - 3600 },
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
        // SPA fallback: rewrite deep client routes under the /dashboard/ base to the
        // index so a hard refresh at /dashboard/apps/foo serves the app (mirrors the
        // Control server's index.html fallback). Registered LAST so it never shadows the
        // /v1/* API mocks above. Assets (with an extension) and Vite's dev-only internals
        // (/@vite, /@react-refresh, /@fs, /src, /node_modules) are left untouched.
        server.middlewares.use((request, response, next) => {
          const url = request.url || "";
          if (request.method !== "GET" || !url.startsWith("/dashboard/")) {
            next();
            return;
          }
          const pathname = url.split("?")[0];
          const rest = pathname.slice("/dashboard/".length);
          const isViteInternal = rest.startsWith("@") || rest.startsWith("src/") || rest.startsWith("node_modules/");
          if (pathname === "/dashboard/" || /\.[a-z0-9]+$/i.test(pathname) || isViteInternal) {
            next();
            return;
          }
          request.url = "/dashboard/";
          next();
        });
      },
    },
  ],
  build: {
    chunkSizeWarningLimit: 600,
    outDir: "../luma/assets/dashboard",
    emptyOutDir: true,
    assetsDir: ".",
    cssCodeSplit: true,
    rollupOptions: {
      output: {
        entryFileNames: "app-[hash].js",
        chunkFileNames: "chunk-[name]-[hash].js",
        assetFileNames: "asset-[name]-[hash][extname]",
      },
    },
  },
});
