import { useCallback, useEffect, useMemo, useState } from "react";
import { Boxes, MapPinned, RefreshCw, ScrollText, UsersRound, WalletCards } from "lucide-react";
import { Badge, CodeCell, PrimaryCell, StatePill } from "../components/ui";
import {
  fetchLaeAdmin,
  type AdminPage,
  type LaeAdminApplication,
  type LaeAdminOperation,
  type LaeAdminPlacement,
  type LaeAdminTenant,
  type LaeAdminUsage,
  type LaeAdminUser,
} from "../laeAdminApi";
import type { Lang } from "../types";
import { PageHeader } from "./PageHeader";

type View = "applications" | "placements" | "users" | "tenants" | "operations" | "usage";
type ResourceState = {
  users: LaeAdminUser[];
  tenants: LaeAdminTenant[];
  applications: LaeAdminApplication[];
  operations: LaeAdminOperation[];
  placements: LaeAdminPlacement[];
  usage: LaeAdminUsage[];
  pages: Record<View, AdminPage>;
};

const emptyPage = { limit: 100, offset: 0, total: 0 };

function bytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const power = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** power).toFixed(power ? 1 : 0)} ${units[power]}`;
}

function time(value?: string | number | null): string {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "-" : parsed.toLocaleString();
}

export function LaeAdminPage({ lang, token }: { lang: Lang; token: string }) {
  const zh = lang === "zh";
  const [view, setView] = useState<View>("applications");
  const [state, setState] = useState<ResourceState>({
    users: [], tenants: [], applications: [], operations: [], placements: [], usage: [],
    pages: { users: emptyPage, tenants: emptyPage, applications: emptyPage, placements: emptyPage, operations: emptyPage, usage: emptyPage },
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError("");
    try {
      const [users, tenants, applications, placements, operations, usage] = await Promise.all([
        fetchLaeAdmin<{ users: LaeAdminUser[]; page: AdminPage }>("users", token, signal),
        fetchLaeAdmin<{ tenants: LaeAdminTenant[]; page: AdminPage }>("tenants", token, signal),
        fetchLaeAdmin<{ applications: LaeAdminApplication[]; page: AdminPage }>("applications", token, signal),
        fetchLaeAdmin<{ placements: LaeAdminPlacement[]; page: AdminPage }>("placements", token, signal),
        fetchLaeAdmin<{ operations: LaeAdminOperation[]; page: AdminPage }>("operations", token, signal),
        fetchLaeAdmin<{ usage: LaeAdminUsage[]; page: AdminPage }>("usage", token, signal),
      ]);
      setState({
        users: users.users || [], tenants: tenants.tenants || [], applications: applications.applications || [],
        placements: placements.placements || [], operations: operations.operations || [], usage: usage.usage || [],
        pages: { users: users.page, tenants: tenants.page, applications: applications.page, placements: placements.page, operations: operations.page, usage: usage.page },
      });
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "AbortError") return;
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const running = useMemo(() => state.applications.filter((app) => app.observedState === "running").length, [state.applications]);
  const failedOperations = useMemo(() => state.operations.filter((operation) => operation.status === "failed").length, [state.operations]);
  const tenantsById = useMemo(() => new Map(state.tenants.map((tenant) => [tenant.id, tenant])), [state.tenants]);
  const tabs: Array<{ id: View; label: string; icon: typeof Boxes }> = [
    { id: "applications", label: zh ? "应用" : "Apps", icon: Boxes },
    { id: "placements", label: zh ? "调度位置" : "Placement", icon: MapPinned },
    { id: "users", label: zh ? "用户" : "Users", icon: UsersRound },
    { id: "tenants", label: zh ? "租户" : "Tenants", icon: WalletCards },
    { id: "operations", label: zh ? "操作" : "Operations", icon: ScrollText },
    { id: "usage", label: zh ? "用量" : "Usage", icon: WalletCards },
  ];

  return (
    <>
      <PageHeader meta={{
        eyebrow: "LUMA APPLICATION ENGINE",
        title: zh ? "LAE 平台总览" : "LAE platform overview",
        description: zh ? "跨租户查看用户、应用、运行状态与资源用量。敏感凭据和值不会进入此视图。" : "Cross-tenant users, applications, runtime state and usage. Credentials and secret values never enter this view.",
        metrics: [
          { label: zh ? "用户" : "Users", value: state.pages.users.total },
          { label: zh ? "租户" : "Tenants", value: state.pages.tenants.total },
          { label: zh ? "运行应用" : "Running", value: `${running}/${state.pages.applications.total}` },
          { label: zh ? "失败操作" : "Failed ops", value: failedOperations },
        ],
        action: <button type="button" className="ghost page-toolbar-cta" disabled={loading} onClick={() => void load()}><RefreshCw size={15} className={loading ? "spin" : ""} />{zh ? "刷新" : "Refresh"}</button>,
      }} />

      {error ? <div className="storage-warnings"><span>{error}</span></div> : null}
      <section className="panel lae-admin-panel">
        <div className="lae-admin-tabs" role="tablist" aria-label="LAE admin resources">
          {tabs.map(({ id, label, icon: Icon }) => (
            <button key={id} type="button" className={view === id ? "active" : ""} onClick={() => setView(id)}>
              <Icon size={15} aria-hidden="true" />{label}<span>{state.pages[id].total}</span>
            </button>
          ))}
        </div>

        {view === "applications" ? <div className="table-wrap"><table><thead><tr><th>{zh ? "应用" : "Application"}</th><th>Tenant</th><th>{zh ? "形态" : "Kind"}</th><th>{zh ? "状态" : "State"}</th><th>{zh ? "服务" : "Services"}</th><th>{zh ? "卷配额" : "Volumes"}</th><th>{zh ? "部署" : "Deployment"}</th></tr></thead><tbody>
          {state.applications.map((app) => {
            const tenant = tenantsById.get(app.tenantId);
            return <tr key={app.id}><td><PrimaryCell title={app.name} meta={app.slug} /></td><td><PrimaryCell title={tenant?.name || app.tenantId} meta={tenant?.ownerEmail || app.tenantId} /></td><td><Badge value={app.kind} /></td><td><StatePill label={`${app.desiredState} / ${app.observedState}`} value={app.observedState} /></td><td>{app.serviceCount}</td><td>{bytes(app.requestedVolumeBytes)}</td><td><CodeCell value={app.currentDeploymentId || "pending"} /></td></tr>;
          })}
          {!state.applications.length ? <tr><td colSpan={7}>{loading ? (zh ? "读取中…" : "Loading…") : (zh ? "暂无应用" : "No applications")}</td></tr> : null}
        </tbody></table></div> : null}

        {view === "users" ? <div className="table-wrap"><table><thead><tr><th>{zh ? "邮箱" : "Email"}</th><th>ID</th><th>{zh ? "状态" : "Status"}</th><th>{zh ? "已验证" : "Verified"}</th><th>{zh ? "最近登录" : "Last login"}</th></tr></thead><tbody>
          {state.users.map((user) => <tr key={user.id}><td><PrimaryCell title={user.email} /></td><td><CodeCell value={user.id} /></td><td><StatePill label={user.status} value={user.status === "active" ? "ready" : user.status} /></td><td>{time(user.emailVerifiedAt)}</td><td>{time(user.lastLoginAt)}</td></tr>)}
          {!state.users.length ? <tr><td colSpan={5}>{loading ? (zh ? "读取中…" : "Loading…") : (zh ? "暂无用户" : "No users")}</td></tr> : null}
        </tbody></table></div> : null}

        {view === "placements" ? <div className="table-wrap"><table><thead><tr><th>{zh ? "运行部署" : "Runtime deployment"}</th><th>{zh ? "租户 / 应用" : "Tenant / app"}</th><th>{zh ? "区域" : "Region"}</th><th>{zh ? "当前节点" : "Active node"}</th><th>{zh ? "候选" : "Candidates"}</th><th>{zh ? "连续性" : "Continuity"}</th><th>{zh ? "更新时间" : "Updated"}</th></tr></thead><tbody>
          {state.placements.map((placement) => <tr key={placement.runtimeDeploymentRef}><td><PrimaryCell title={placement.status} meta={placement.runtimeDeploymentRef} /></td><td><PrimaryCell title={placement.applicationRef} meta={placement.tenantRef} /></td><td><Badge value={placement.region || "unknown"} /></td><td>{placement.activeAllocations.length ? placement.activeAllocations.map((allocation) => <PrimaryCell key={allocation.allocationId || allocation.nodeId} title={allocation.nodeName || allocation.nodeId} meta={allocation.status} />) : <StatePill label={placement.observationStatus} value={placement.observationStatus} />}</td><td><PrimaryCell title={`${placement.candidateNodeIds.length}`} meta={placement.candidateNodeIds.join(", ") || "-"} /></td><td><StatePill label={placement.continuity} value={placement.continuity} /></td><td>{time(placement.updatedAt ? placement.updatedAt * 1000 : null)}</td></tr>)}
          {!state.placements.length ? <tr><td colSpan={7}>{loading ? (zh ? "读取中…" : "Loading…") : (zh ? "暂无运行部署" : "No runtime placements")}</td></tr> : null}
        </tbody></table></div> : null}

        {view === "tenants" ? <div className="table-wrap"><table><thead><tr><th>{zh ? "租户" : "Tenant"}</th><th>{zh ? "所有者" : "Owner"}</th><th>{zh ? "套餐" : "Plan"}</th><th>{zh ? "状态" : "Status"}</th><th>{zh ? "创建时间" : "Created"}</th></tr></thead><tbody>
          {state.tenants.map((tenant) => <tr key={tenant.id}><td><PrimaryCell title={tenant.name} meta={tenant.slug} /></td><td><PrimaryCell title={tenant.ownerEmail} meta={tenant.id} /></td><td><Badge value={(tenant.plan || "unknown").toUpperCase()} /></td><td><StatePill label={tenant.status} value={tenant.status === "active" ? "ready" : tenant.status} /></td><td>{time(tenant.createdAt)}</td></tr>)}
          {!state.tenants.length ? <tr><td colSpan={5}>{loading ? (zh ? "读取中…" : "Loading…") : (zh ? "暂无租户" : "No tenants")}</td></tr> : null}
        </tbody></table></div> : null}

        {view === "operations" ? <div className="table-wrap"><table><thead><tr><th>{zh ? "操作" : "Operation"}</th><th>Tenant</th><th>{zh ? "目标" : "Target"}</th><th>{zh ? "阶段" : "Phase"}</th><th>{zh ? "状态" : "Status"}</th><th>{zh ? "时间" : "Time"}</th></tr></thead><tbody>
          {state.operations.map((operation) => <tr key={operation.id}><td><PrimaryCell title={operation.kind} meta={operation.id} /></td><td><CodeCell value={operation.tenantId} /></td><td><CodeCell value={operation.targetId} /></td><td>{operation.phase || "-"}</td><td><StatePill label={operation.errorCode || operation.status} value={operation.status} /></td><td>{time(operation.createdAt)}</td></tr>)}
          {!state.operations.length ? <tr><td colSpan={6}>{loading ? (zh ? "读取中…" : "Loading…") : (zh ? "暂无操作" : "No operations")}</td></tr> : null}
        </tbody></table></div> : null}

        {view === "usage" ? <div className="table-wrap"><table><thead><tr><th>Tenant</th><th>{zh ? "应用" : "Apps"}</th><th>{zh ? "托管卷" : "Managed volumes"}</th><th>{zh ? "上传代码" : "Stored uploads"}</th><th>{zh ? "合计" : "Total"}</th></tr></thead><tbody>
          {state.usage.map((usage) => <tr key={usage.tenantId}><td><CodeCell value={usage.tenantId} /></td><td>{usage.applicationCount}</td><td>{bytes(usage.requestedVolumeBytes)}</td><td>{bytes(usage.storedUploadBytes)}</td><td><strong>{bytes(usage.requestedVolumeBytes + usage.storedUploadBytes)}</strong></td></tr>)}
          {!state.usage.length ? <tr><td colSpan={5}>{loading ? (zh ? "读取中…" : "Loading…") : (zh ? "暂无用量" : "No usage")}</td></tr> : null}
        </tbody></table></div> : null}
      </section>
    </>
  );
}
