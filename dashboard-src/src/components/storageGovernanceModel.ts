export function storageTime(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const ms = typeof value === "number" ? (value < 1e12 ? value * 1000 : value) : Date.parse(value);
  return Number.isFinite(ms) && ms > 0 ? ms : null;
}
export function storageBytes(value: number | null | undefined, unknown: string): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return unknown;
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = value, unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++; }
  return `${size.toFixed(unit && size < 10 ? 1 : 0)} ${units[unit]}`;
}
export function cleanupPlanGate(plan: { planId?: string; expiresAt?: string | number; eligibleAfter?: string | number; blockedReasons?: string[] } | null, now = Date.now()): "missing" | "blocked" | "expired" | "grace" | "ready" {
  if (!plan?.planId) return "missing";
  if (plan.blockedReasons?.length) return "blocked";
  const expires = storageTime(plan.expiresAt);
  if (expires === null || storageTime(plan.eligibleAfter) === null) return "missing";
  if (expires !== null && now >= expires) return "expired";
  const eligible = storageTime(plan.eligibleAfter);
  if (eligible !== null && now < eligible) return "grace";
  return "ready";
}
export function storageTaskFinished(status: string): boolean {
  return ["done", "completed", "succeeded", "failed", "error", "cancelled", "expired"].includes(status);
}

export function storageCategory(id: string, fallback: string, zh: boolean): string {
  if (!zh) return fallback;
  return ({ database: "Manager 数据库", databaseWal: "SQLite 写入日志", databaseShm: "SQLite 共享内存", legacyConfig: "迁移前配置快照", metrics: "本地指标历史", migrationBackups: "迁移备份", builder: "Builder 源码与分析产物", registry: "Registry 镜像", buildkit: "BuildKit 缓存", trivy: "Trivy 缓存", volumes: "应用数据卷" } as Record<string, string>)[id] || fallback;
}
