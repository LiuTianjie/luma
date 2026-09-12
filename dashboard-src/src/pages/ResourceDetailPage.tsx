import { ArrowLeft, Server, SquareTerminal } from "lucide-react";
import { StatePill } from "../components/primitives";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Progress } from "@/components/ui/progress";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { localizeState } from "../i18n";
import { useRouter, toHref } from "../router";
import { servicePath } from "../objectRoutes";
import { PageHeader } from "./PageHeader";
import type { DashboardNode, DashboardService, Lang } from "../types";

function formatBytes(value?: number): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "-";
  if (value === 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  const unit = Math.min(Math.max(0, Math.floor(Math.log(value) / Math.log(1024))), units.length - 1);
  return `${Number((value / 1024 ** unit).toFixed(unit === 0 ? 0 : 1))} ${units[unit]}`;
}

function formatPercent(value?: number): string {
  return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(1)}%` : "-";
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="m-0 min-w-0 text-sm wrap-break-word">{value || "-"}</dd>
    </div>
  );
}

export function ResourceDetailPage({ lang, node, service, services, applicationNames, onTerminal }: {
  lang: Lang; node?: DashboardNode; service?: DashboardService; services: DashboardService[]; applicationNames: string[]; onTerminal: () => void;
}) {
  const { navigate } = useRouter();
  const zh = lang === "zh";
  const back = node ? "/fleet" : service?.stack && applicationNames.includes(service.stack) ? `/apps/${encodeURIComponent(service.stack)}/services` : "/apps";
  const nodeNames = new Set(node ? [node.name, node.hostname, node.displayName].filter((name): name is string => Boolean(name)) : []);
  const onNode = node ? services.filter((item) => [item.node, ...(item.nodes || []), ...(item.tasks || []).map((task) => task.node), ...(item.resources?.actual?.nodes || [])].some((name) => Boolean(name) && nodeNames.has(name!))) : [];
  const cpu = node?.metrics?.cpuPercent ?? node?.metrics?.loadPercent;
  const memory = node?.metrics?.memoryUsedPercent;
  const title = node?.displayName || node?.name || (service?.stack ? `${service.stack} / ${service.name}` : service?.name) || "-";
  const status = node?.state || service?.status || service?.health || "unknown";

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader meta={{
        eyebrow: node ? (zh ? "节点详情" : "Node details") : (zh ? "服务详情" : "Service details"),
        title,
        description: node
          ? [node.agentOs, node.agentStatus, node.terminalConnected ? (zh ? "Shell 已连接" : "Shell connected") : (zh ? "Shell 未连接" : "Shell waiting")].filter(Boolean).join(" · ")
          : [service?.fullName, service?.region, service?.image].filter(Boolean).join(" · "),
        metrics: [],
        action: (
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              nativeButton={false}
              render={<a href={toHref(back)} onClick={(event) => { if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); navigate(back); } }} />}
            >
              <ArrowLeft data-icon="inline-start" />
              {zh ? "返回列表" : "Back to list"}
            </Button>
            <Button disabled={node ? !node.terminalConnected : !service} onClick={onTerminal}>
              <SquareTerminal data-icon="inline-start" />
              {zh ? "进入 Shell" : "Open shell"}
            </Button>
          </div>
        ),
      }} />

      {node ? (
        <div className="grid gap-4 md:grid-cols-3">
          <Card size="sm">
            <CardHeader>
              <CardTitle>CPU</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <p className="text-2xl font-semibold tabular-nums">{formatPercent(cpu)}</p>
              {typeof cpu === "number" && Number.isFinite(cpu) ? <Progress aria-label={zh ? "CPU 使用率" : "CPU usage"} value={Math.max(0, Math.min(100, cpu))} /> : <p className="text-sm text-muted-foreground">{zh ? "暂无使用率数据" : "Usage data unavailable"}</p>}
            </CardContent>
          </Card>
          <Card size="sm">
            <CardHeader>
              <CardTitle>{zh ? "内存" : "Memory"}</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <p className="text-2xl font-semibold tabular-nums">{formatPercent(memory)}</p>
              {typeof memory === "number" && Number.isFinite(memory) ? <Progress aria-label={zh ? "内存使用率" : "Memory usage"} value={Math.max(0, Math.min(100, memory))} /> : <p className="text-sm text-muted-foreground">{zh ? "暂无使用率数据" : "Usage data unavailable"}</p>}
              <p className="text-sm text-muted-foreground">{zh ? "总量" : "Total"} {formatBytes(node.metrics?.memoryTotalBytes ?? node.capacity?.memoryBytes)}</p>
            </CardContent>
          </Card>
          <Card size="sm">
            <CardHeader>
              <CardTitle>{zh ? "负载 / 容量" : "Load / capacity"}</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <p className="text-2xl font-semibold tabular-nums">{typeof node.metrics?.load1 === "number" && Number.isFinite(node.metrics.load1) ? node.metrics.load1.toFixed(2) : "-"}</p>
              <p className="text-sm text-muted-foreground">{zh ? "CPU 容量" : "CPU capacity"} {node.capacity?.cpus ?? "-"} · {zh ? "内存容量" : "Memory"} {formatBytes(node.capacity?.memoryBytes)}</p>
            </CardContent>
          </Card>
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>{zh ? "基本信息" : "Overview"}</CardTitle>
          <CardAction className="flex flex-wrap items-center justify-end gap-2">
            <StatePill value={status} label={localizeState(lang, status)} />
            {node?.role ? <Badge variant="secondary">{node.role}</Badge> : null}
            {node?.region ? <Badge variant="outline">{node.region}</Badge> : null}
            {service?.exposure ? <Badge variant="outline">{service.exposure}</Badge> : null}
          </CardAction>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 md:grid-cols-3">
            {node ? (
              <>
                <Fact label={zh ? "名称" : "Name"} value={node.name || "-"} />
                <Fact label={zh ? "显示名" : "Display name"} value={node.displayName || node.name || "-"} />
                <Fact label={zh ? "区域" : "Region"} value={node.region || "-"} />
                <Fact label={zh ? "可用性" : "Availability"} value={node.availability || "-"} />
                <Fact label="Leader" value={node.leader ? (zh ? "是" : "true") : (zh ? "否" : "false")} />
                <Fact label="Agent" value={[node.agentStatus, node.agentOs].filter(Boolean).join(" / ") || "-"} />
              </>
            ) : service ? (
              <>
                <Fact label={zh ? "服务" : "Service"} value={service.name || "-"} />
                <Fact label={zh ? "应用" : "Application"} value={service.stack || "-"} />
                <Fact label={zh ? "区域" : "Region"} value={service.region || "-"} />
                <Fact label={zh ? "入口" : "Exposure"} value={service.exposure || "-"} />
                <Fact label={zh ? "副本" : "Replicas"} value={`${service.running ?? 0}/${service.desired ?? 0}`} />
                <Fact label={zh ? "节点" : "Nodes"} value={(service.nodes || []).join(", ") || service.node || "-"} />
                <Fact label={zh ? "镜像" : "Image"} value={service.image || "-"} />
              </>
            ) : null}
          </dl>
        </CardContent>
      </Card>

      {node ? (
        <Card>
          <CardHeader>
            <CardTitle>{zh ? "运行服务" : "Workloads"}</CardTitle>
            <CardDescription>{zh ? `${onNode.length} 个服务跑在这台节点上` : `${onNode.length} services on this node`}</CardDescription>
          </CardHeader>
          <CardContent>
            {onNode.length ? <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{zh ? "应用 / 服务" : "Application / service"}</TableHead>
                  <TableHead>{zh ? "状态" : "Status"}</TableHead>
                  <TableHead>{zh ? "副本" : "Replicas"}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {onNode.map((item) => (
                  <TableRow key={item.fullName || item.name}>
                    <TableCell>
                      <Button
                        variant="link"
                        size="sm"
                        className="h-auto max-w-96 justify-start px-0"
                        nativeButton={false}
                        render={<a href={toHref(servicePath(item.fullName || item.name || ""))} onClick={(event) => { if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); navigate(servicePath(item.fullName || item.name || "")); } }} />}
                      >
                        <span className="truncate" title={item.stack ? `${item.stack} / ${item.name}` : item.name}>{item.stack ? `${item.stack} / ` : ""}{item.name}</span>
                      </Button>
                    </TableCell>
                    <TableCell>
                      <StatePill value={item.status || item.health || "unknown"} label={localizeState(lang, item.status || item.health || "unknown")} />
                    </TableCell>
                    <TableCell>{item.running ?? 0}/{item.desired ?? 0}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table> : (
              <Empty>
                <EmptyHeader>
                  <EmptyMedia variant="icon"><Server /></EmptyMedia>
                  <EmptyTitle>{zh ? "当前没有关联的服务" : "No associated services"}</EmptyTitle>
                  <EmptyDescription>{zh ? "服务调度到这台节点后会显示在这里。" : "Services appear here once they are scheduled on this node."}</EmptyDescription>
                </EmptyHeader>
              </Empty>
            )}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
