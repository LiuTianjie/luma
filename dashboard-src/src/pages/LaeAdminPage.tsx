import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

import { Boxes, CloudCog, MapPinned, RefreshCw, ScrollText, UsersRound, WalletCards } from "lucide-react";
import { CodeCell, PrimaryCell, StatePill } from "../components/primitives";
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
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { AlertCircle } from "lucide-react";

type View = "applications" | "placements" | "users" | "tenants" | "operations" | "usage";
type ResourceState = {
  token: string;
  users: LaeAdminUser[];
  tenants: LaeAdminTenant[];
  applications: LaeAdminApplication[];
  operations: LaeAdminOperation[];
  placements: LaeAdminPlacement[];
  usage: LaeAdminUsage[];
  pages: Partial<Record<View, AdminPage>>;
  loading: Partial<Record<View, boolean>>;
  errors: Partial<Record<View, string>>;
};

function emptyState(token: string): ResourceState {
  return { token, users: [], tenants: [], applications: [], operations: [], placements: [], usage: [], pages: {}, loading: {}, errors: {} };
}

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

function isLaeUnavailable(message?: string) {
  return /LAE admin API is unavailable/i.test(message || "");
}

export function LaeAdminPage({ lang, token }: { lang: Lang; token: string }) {
  const zh = lang === "zh";
  const [view, setView] = useState<View>("applications");
  const [snapshot, setState] = useState<ResourceState>(() => emptyState(token));
  // A changed login must never render rows or counters from the previous token.
  const state = snapshot.token === token ? snapshot : emptyState(token);
  const cache = useRef(state);
  cache.current = state;
  const requests = useRef<Partial<Record<View, AbortController>>>({});
  const loading = Boolean(state.loading[view]) || (!state.pages[view] && !state.errors[view]);

  const load = useCallback(async (resource: View) => {
    requests.current[resource]?.abort();
    const controller = new AbortController();
    requests.current[resource] = controller;
    const timeout = window.setTimeout(() => controller.abort(), 20000);
    const current = () => requests.current[resource] === controller;
    setState((previous) => {
      const next = previous.token === token ? previous : emptyState(token);
      return { ...next, loading: { ...next.loading, [resource]: true }, errors: { ...next.errors, [resource]: "" } };
    });
    try {
      const result = await fetchLaeAdmin<Partial<ResourceState> & { page?: AdminPage }>(resource, token, controller.signal);
      if (!current() || controller.signal.aborted) return;
      const rows = result[resource] || [];
      setState((previous) => previous.token !== token ? previous : {
        ...previous,
        [resource]: rows,
        pages: { ...previous.pages, [resource]: result.page || { limit: 100, offset: 0, total: rows.length } },
      });
    } catch (caught) {
      if (!current()) return;
      setState((previous) => previous.token !== token ? previous : {
        ...previous,
        errors: { ...previous.errors, [resource]: controller.signal.aborted ? (lang === "zh" ? "请求超时，请重试。" : "Request timed out. Try again.") : caught instanceof Error ? caught.message : String(caught) },
      });
    } finally {
      window.clearTimeout(timeout);
      if (current()) {
        delete requests.current[resource];
        setState((previous) => previous.token !== token ? previous : { ...previous, loading: { ...previous.loading, [resource]: false } });
      }
    }
  }, [token, lang]);

  useEffect(() => {
    if (!cache.current.pages[view]) void load(view);
    // Tenant names enrich app rows independently; their latency never holds the
    // application response or the visible table behind a Promise.all barrier.
    if (view === "applications" && !cache.current.pages.tenants) void load("tenants");
    return () => {
      const canceled = Object.keys(requests.current) as View[];
      for (const resource of canceled) requests.current[resource]?.abort();
      requests.current = {};
      setState((previous) => previous.token !== token ? previous : {
        ...previous, loading: { ...previous.loading, ...Object.fromEntries(canceled.map((resource) => [resource, false])) },
      });
    };
  }, [load, token, view]);

  const refresh = () => {
    void load(view);
    if (view === "applications") void load("tenants");
  };

  const running = useMemo(() => state.applications.filter((app) => app.observedState === "running").length, [state.applications]);
  const failedOperations = useMemo(() => state.operations.filter((operation) => operation.status === "failed").length, [state.operations]);
  const unavailable = isLaeUnavailable(state.errors[view]);
  const tenantsById = useMemo(() => new Map(state.tenants.map((tenant) => [tenant.id, tenant])), [state.tenants]);
  const tabs: Array<{ id: View; label: string; icon: typeof Boxes }> = [
    { id: "applications", label: zh ? "应用" : "Apps", icon: Boxes },
    { id: "placements", label: zh ? "调度位置" : "Placement", icon: MapPinned },
    { id: "users", label: zh ? "用户" : "Users", icon: UsersRound },
    { id: "tenants", label: zh ? "租户" : "Tenants", icon: WalletCards },
    { id: "operations", label: zh ? "操作" : "Operations", icon: ScrollText },
    { id: "usage", label: zh ? "用量" : "Usage", icon: WalletCards },
  ];

  const tables: Record<View, { headings: string[]; rows: () => ReactNode; description: string; empty: string }> = {
    applications: {
      headings: [zh ? "应用" : "Application", zh ? "租户" : "Tenant", zh ? "形态" : "Kind", zh ? "状态" : "State", zh ? "服务" : "Services", zh ? "卷配额" : "Volumes", zh ? "部署" : "Deployment"],
      description: zh ? "查看各租户应用的运行状态、服务和存储配额。" : "Application state, services, and storage quotas across tenants.",
      empty: zh ? "暂无应用" : "No applications",
      rows: () => state.applications.map((app) => {
        const tenant = tenantsById.get(app.tenantId);
        return <TableRow key={app.id}>
          <TableCell><PrimaryCell title={app.name} meta={app.slug} /></TableCell>
          <TableCell><PrimaryCell title={tenant?.name || app.tenantId} meta={tenant?.ownerEmail || app.tenantId} /></TableCell>
          <TableCell><Badge variant="secondary">{app.kind}</Badge></TableCell>
          <TableCell><StatePill label={`${app.desiredState} / ${app.observedState}`} value={app.observedState} /></TableCell>
          <TableCell>{app.serviceCount}</TableCell>
          <TableCell>{bytes(app.requestedVolumeBytes)}</TableCell>
          <TableCell><CodeCell value={app.currentDeploymentId || "pending"} /></TableCell>
        </TableRow>;
      }),
    },
    placements: {
      headings: [zh ? "运行部署" : "Runtime deployment", zh ? "租户 / 应用" : "Tenant / app", zh ? "区域" : "Region", zh ? "当前节点" : "Active node", zh ? "候选" : "Candidates", zh ? "连续性" : "Continuity", zh ? "更新时间" : "Updated"],
      description: zh ? "查看部署的区域、当前节点和调度候选。" : "Deployment regions, current nodes, and scheduling candidates.",
      empty: zh ? "暂无运行部署" : "No runtime placements",
      rows: () => state.placements.map((placement) => <TableRow key={placement.runtimeDeploymentRef}>
        <TableCell><PrimaryCell title={placement.status} meta={placement.runtimeDeploymentRef} /></TableCell>
        <TableCell><PrimaryCell title={placement.applicationRef} meta={placement.tenantRef} /></TableCell>
        <TableCell><Badge variant="secondary">{placement.region || "unknown"}</Badge></TableCell>
        <TableCell>{placement.activeAllocations.length ? <div className="flex flex-col gap-2">{placement.activeAllocations.map((allocation) => <PrimaryCell key={allocation.allocationId || allocation.nodeId} title={allocation.nodeName || allocation.nodeId} meta={allocation.status} />)}</div> : <StatePill label={placement.observationStatus} value={placement.observationStatus} />}</TableCell>
        <TableCell><PrimaryCell title={`${placement.candidateNodeIds.length}`} meta={placement.candidateNodeIds.join(", ") || "-"} /></TableCell>
        <TableCell><StatePill label={placement.continuity} value={placement.continuity} /></TableCell>
        <TableCell>{time(placement.updatedAt ? placement.updatedAt * 1000 : null)}</TableCell>
      </TableRow>),
    },
    users: {
      headings: [zh ? "邮箱" : "Email", "ID", zh ? "状态" : "Status", zh ? "验证时间" : "Verified", zh ? "最近登录" : "Last login"],
      description: zh ? "查看平台用户及其邮箱验证和登录状态。" : "Platform users, email verification, and login activity.",
      empty: zh ? "暂无用户" : "No users",
      rows: () => state.users.map((user) => <TableRow key={user.id}>
        <TableCell><PrimaryCell title={user.email} /></TableCell>
        <TableCell><CodeCell value={user.id} /></TableCell>
        <TableCell><StatePill label={user.status} value={user.status === "active" ? "ready" : user.status} /></TableCell>
        <TableCell>{time(user.emailVerifiedAt)}</TableCell>
        <TableCell>{time(user.lastLoginAt)}</TableCell>
      </TableRow>),
    },
    tenants: {
      headings: [zh ? "租户" : "Tenant", zh ? "所有者" : "Owner", zh ? "套餐" : "Plan", zh ? "状态" : "Status", zh ? "创建时间" : "Created"],
      description: zh ? "查看租户、所有者和当前套餐。" : "Tenant accounts, owners, and current plans.",
      empty: zh ? "暂无租户" : "No tenants",
      rows: () => state.tenants.map((tenant) => <TableRow key={tenant.id}>
        <TableCell><PrimaryCell title={tenant.name} meta={tenant.slug} /></TableCell>
        <TableCell><PrimaryCell title={tenant.ownerEmail} meta={tenant.id} /></TableCell>
        <TableCell><Badge variant="secondary">{(tenant.plan || "unknown").toUpperCase()}</Badge></TableCell>
        <TableCell><StatePill label={tenant.status} value={tenant.status === "active" ? "ready" : tenant.status} /></TableCell>
        <TableCell>{time(tenant.createdAt)}</TableCell>
      </TableRow>),
    },
    operations: {
      headings: [zh ? "操作" : "Operation", zh ? "租户" : "Tenant", zh ? "目标" : "Target", zh ? "阶段" : "Phase", zh ? "状态" : "Status", zh ? "时间" : "Time"],
      description: zh ? "查看跨租户操作及其执行阶段和结果。" : "Cross-tenant operations, execution phases, and results.",
      empty: zh ? "暂无操作" : "No operations",
      rows: () => state.operations.map((operation) => <TableRow key={operation.id}>
        <TableCell><PrimaryCell title={operation.kind} meta={operation.id} /></TableCell>
        <TableCell><CodeCell value={operation.tenantId} /></TableCell>
        <TableCell><CodeCell value={operation.targetId} /></TableCell>
        <TableCell>{operation.phase || "-"}</TableCell>
        <TableCell><StatePill label={operation.errorCode || operation.status} value={operation.status} /></TableCell>
        <TableCell>{time(operation.createdAt)}</TableCell>
      </TableRow>),
    },
    usage: {
      headings: [zh ? "租户" : "Tenant", zh ? "应用" : "Apps", zh ? "托管卷" : "Managed volumes", zh ? "上传代码" : "Stored uploads", zh ? "合计" : "Total"],
      description: zh ? "查看各租户的应用数量、存储配额和代码占用。" : "Application counts, storage quotas, and stored code by tenant.",
      empty: zh ? "暂无用量" : "No usage",
      rows: () => state.usage.map((usage) => <TableRow key={usage.tenantId}>
        <TableCell><CodeCell value={usage.tenantId} /></TableCell>
        <TableCell>{usage.applicationCount}</TableCell>
        <TableCell>{bytes(usage.requestedVolumeBytes)}</TableCell>
        <TableCell>{bytes(usage.storedUploadBytes)}</TableCell>
        <TableCell>{bytes(usage.requestedVolumeBytes + usage.storedUploadBytes)}</TableCell>
      </TableRow>),
    },
  };

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader meta={{
        eyebrow: "LUMA APPLICATION ENGINE",
        title: zh ? "LAE 平台总览" : "LAE platform overview",
        description: unavailable
          ? (zh ? "LAE 是可选能力，默认不随首次安装启用。当前集群没有配置 LAE 管理入口。" : "LAE is optional and is not part of first install. This cluster has no LAE admin endpoint configured.")
          : (zh ? "跨租户查看用户、应用、运行状态与资源用量。敏感凭据和值不会进入此视图。" : "Cross-tenant users, applications, runtime state and usage. Credentials and secret values never enter this view."),
        metrics: [
          { label: zh ? "用户" : "Users", value: state.pages.users?.total ?? "—" },
          { label: zh ? "租户" : "Tenants", value: state.pages.tenants?.total ?? "—" },
          { label: zh ? "运行应用（当前页）" : "Running (loaded page)", value: state.pages.applications ? running : "—" },
          { label: zh ? "失败操作（当前页）" : "Failed ops (loaded page)", value: state.pages.operations ? failedOperations : "—" },
        ],
        action: <Button variant="outline" type="button" size="sm" disabled={loading} onClick={refresh}>
          {loading ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <RefreshCw data-icon="inline-start" />}{zh ? "刷新" : "Refresh"}
        </Button>,
      }} />

      {unavailable ? (
        <Empty>
          <EmptyHeader>
            <EmptyMedia variant="icon"><CloudCog /></EmptyMedia>
            <EmptyTitle>{zh ? "未启用 LAE" : "LAE is not enabled"}</EmptyTitle>
            <EmptyDescription>
              {zh
                ? "开箱安装只需要 Luma Control、Traefik 和 Nomad。要接入多租户 LAE，请按 docs/lae/ 单独配置后再刷新。"
                : "A first Luma install only needs Control, Traefik, and Nomad. Enable LAE later from docs/lae/, then refresh."}
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : null}

      {unavailable ? null : <Tabs value={view} onValueChange={(value) => { if (tabs.some((tab) => tab.id === value)) setView(value as View); }} className="min-w-0 gap-6">
        <div className="max-w-full overflow-x-auto">
          <TabsList aria-label={zh ? "LAE 平台资源" : "LAE platform resources"}>
            {tabs.map(({ id, label, icon: Icon }) => (
              <TabsTrigger key={id} value={id}>
                <Icon data-icon="inline-start" />{label}<Badge variant="secondary">{state.pages[id]?.total ?? "—"}</Badge>
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
        {tabs.map(({ id, label, icon: Icon }) => {
          const page = state.pages[id];
          const resourceLoading = Boolean(state.loading[id]) || (!page && !state.errors[id]);
          const resourceError = state.errors[id];
          const count = state[id].length;
          const table = tables[id];
          return <TabsContent key={id} value={id}>
            {id === view ? <Card>
              <CardHeader>
                <CardTitle>{label}</CardTitle>
                <CardDescription>{table.description}</CardDescription>
              </CardHeader>
              <CardContent className="flex min-w-0 flex-col gap-4" aria-busy={resourceLoading}>
                {resourceError ? <Alert variant="destructive">
                  <AlertCircle />
                  <AlertTitle>{zh ? "读取失败" : "Load failed"}</AlertTitle>
                  <AlertDescription>
                    <p>{resourceError}</p>
                    <Button variant="outline" size="sm" type="button" disabled={resourceLoading} onClick={() => void load(id)}>
                      {resourceLoading ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <RefreshCw data-icon="inline-start" />}{zh ? "重试" : "Retry"}
                    </Button>
                  </AlertDescription>
                </Alert> : null}
                {resourceLoading && !page ? (
                  <div className="flex flex-col gap-4" role="status" aria-label={zh ? `加载${label}…` : `Loading ${label}…`}>
                    <Skeleton className="h-5 w-40" />
                    <Skeleton className="h-10 w-full" />
                    <Skeleton className="h-10 w-full" />
                    <Skeleton className="h-10 w-3/4" />
                    <span className="sr-only">{zh ? `加载${label}…` : `Loading ${label}…`}</span>
                  </div>
                ) : count ? (
                  <Table aria-label={label} containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? `${label}，可横向滚动` : `${label}, horizontally scrollable` }}>
                    <TableHeader><TableRow>{table.headings.map((heading) => <TableHead key={heading}>{heading}</TableHead>)}</TableRow></TableHeader>
                    <TableBody>{table.rows()}</TableBody>
                  </Table>
                ) : !resourceError ? (
                  <Empty>
                    <EmptyHeader>
                      <EmptyMedia variant="icon"><Icon /></EmptyMedia>
                      <EmptyTitle>{table.empty}</EmptyTitle>
                      <EmptyDescription>{zh ? "此资源当前没有可显示的记录。" : "There are no records for this resource yet."}</EmptyDescription>
                    </EmptyHeader>
                  </Empty>
                ) : null}
              </CardContent>
              {page ? <CardFooter>
                <span>{zh ? `已显示 ${count} 条，共 ${page.total} 条` : `${count} records shown, ${page.total} total`}</span>
              </CardFooter> : null}
            </Card> : null}
          </TabsContent>;
        })}
      </Tabs>}
    </div>
  );
}
