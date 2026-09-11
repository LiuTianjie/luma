import { useState, type MouseEvent } from "react";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { NodeFleetMap } from "../components/NodeFleetMap";
import { RegionPanel } from "../components/RegionPanel";
import { NodeTopology } from "../components/NodeTopology";
import { SystemUpdatePanel } from "../components/SystemUpdatePanel";
import { TrafficPaths } from "../components/TrafficPaths";
import { InfrastructureNavigation } from "../components/InfrastructureNavigation";
import { toHref, useRouter } from "../router";
import type { DashboardNode, Lang } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";
import "./InfrastructureWorkspace.css";

function readyNode(node: DashboardNode) {
  return (node.state || "").toLowerCase() === "ready" && (node.availability || "").toLowerCase() !== "drain";
}

function managerNode(node: DashboardNode) {
  return (node.role || "").toLowerCase().includes("manager") || Boolean(node.leader);
}

function agentReady(node: DashboardNode) {
  return (node.agentStatus || "").toLowerCase() === "ready";
}

function joinCommand(nodeJoin?: { token?: string; domain?: string }, region?: string) {
  const rawOrigin = nodeJoin?.domain || (typeof window !== "undefined" ? window.location.origin : "https://<control-domain>");
  const origin = /^https?:\/\//i.test(rawOrigin) ? rawOrigin : `https://${rawOrigin}`;
  const token = nodeJoin?.token?.trim() || "";
  if (!token) return "";
  return `luma node join ${origin} --token ${token} --region ${region || "<region>"} --name <node-name>`;
}

export function NodesPage({
  lang,
  vm,
  theme,
  token,
  nodeJoin,
  onSelectNode,
  onTerminal,
  onRefresh,
  controlVersion,
}: {
  lang: Lang;
  vm: DashboardViewModel;
  theme: "light" | "dark";
  token: string;
  nodeJoin?: { token?: string; domain?: string };
  onSelectNode: (node: DashboardNode) => void;
  onTerminal: (node: DashboardNode) => void;
  onRefresh: () => Promise<void> | void;
  controlVersion: string;
}) {
  const zh = lang === "zh";
  const { path, navigate } = useRouter();
  const requestedSection = path.split("/")[2] || "nodes";
  const section = ["nodes", "join", "regions", "maintenance", "network"].includes(requestedSection) ? requestedSection : "unknown";
  const ready = vm.nodes.filter(readyNode).length;
  const managers = vm.nodes.filter(managerNode).length;
  const agents = vm.nodes.filter(agentReady).length;
  const terminalNodes = vm.nodes.filter((node) => node.terminalConnected).length;
  const defaultRegion = vm.regions.find((item) => !item.builtin)?.name || vm.regions[0]?.name;
  const command = joinCommand(nodeJoin, defaultRegion);
  const joinTokenAvailable = Boolean(nodeJoin?.token?.trim());
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);

  const copyCommand = async () => {
    setCopyError(false);
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopyError(true);
    }
  };

  return (
    <div className="infrastructure-workspace">
      <InfrastructureNavigation lang={lang} />
      <PageHeader
        meta={{
          eyebrow: zh ? "节点舰队" : "Fleet",
          title: section === "join" ? (zh ? "加入节点" : "Join a node") : section === "regions" ? (zh ? "区域管理" : "Regions") : section === "maintenance" ? (zh ? "系统维护" : "System maintenance") : section === "network" ? (zh ? "网络与拓扑" : "Network and topology") : (zh ? "节点" : "Nodes"),
          description: section === "join" ? (zh ? "在目标机器上安装并接入当前集群。" : "Connect a host to this cluster.")
            : section === "regions" ? (zh ? "管理调度区域及其出口策略。" : "Manage scheduling regions and egress policies.")
            : section === "maintenance" ? (zh ? "检查路由，升级控制面和节点，并追踪任务结果。" : "Check routes, upgrade the control plane and agents, and track results.")
            : section === "network" ? (zh ? "查看入口流量路径、证书和节点拓扑。" : "Inspect ingress paths, certificates, and node topology.")
            : (zh ? "查看节点资源、调度状态与终端可用性。" : "Inspect node resources, scheduling state, and terminal availability."),
          metrics: section === "nodes" ? [
            { label: zh ? "Ready 节点" : "Ready nodes", value: `${ready}/${vm.nodes.length}` },
            { label: zh ? "Ready Agent" : "Ready agents", value: `${agents}/${vm.nodes.length}` },
            { label: zh ? "Manager" : "Managers", value: managers },
            { label: "Terminal", value: terminalNodes },
          ] : [],
        }}
      />

      {section !== "network" ? (
        <Tabs value={section}>
          <TabsList aria-label={zh ? "节点管理" : "Node management"}>
            {[
              ["nodes", "/fleet", zh ? "节点列表" : "All nodes"],
              ["join", "/fleet/join", zh ? "加入节点" : "Join node"],
              ["regions", "/fleet/regions", zh ? "区域" : "Regions"],
              ["maintenance", "/fleet/maintenance", zh ? "系统维护" : "Maintenance"],
            ].map(([key, href, label]) => (
              <TabsTrigger
                key={key}
                value={key}
                render={<a href={toHref(href)} aria-current={section === key ? "page" : undefined} />}
                onClick={(event: MouseEvent<HTMLElement>) => {
                  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                  event.preventDefault();
                  navigate(href);
                }}
              >
                {label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      ) : null}

      {section === "unknown" && <div className="empty-inline"><p>{zh ? "此基础设施页面不存在。" : "This infrastructure page does not exist."}</p><Button type="button" onClick={() => navigate("/fleet")}>{zh ? "返回节点列表" : "Back to nodes"}</Button></div>}
      {section === "nodes" && <NodeFleetMap lang={lang} nodes={vm.nodes} services={vm.services} onSelect={onSelectNode} onTerminal={onTerminal} />}
      {section === "regions" && <RegionPanel lang={lang} token={token} regions={vm.regions} nodes={vm.nodes} onRefresh={onRefresh} />}

      {section === "join" && (
        <Card>
          <CardHeader>
            <CardTitle>{zh ? "在目标机器上执行" : "Run on the target host"}</CardTitle>
            <CardDescription>
              {zh
              ? joinTokenAvailable
                ? "命令已包含当前集群的节点加入 Token 和可用区域。把 <region> 或当前区域按需调整，把 <node-name> 换成节点名后在目标机器执行。"
                : "控制面暂时没有返回节点加入 Token。请刷新 Dashboard；如果仍然缺失，请先升级 Manager 控制面。"
              : joinTokenAvailable
                ? "The command includes this cluster's node join token and an available region. Adjust the region if needed and replace <node-name> with the host name."
                : "Control did not return a node join token. Refresh the Dashboard; if it remains missing, update the Manager control plane first."}
            </CardDescription>
            <CardAction>
              <Button variant="outline" size="sm" disabled={!joinTokenAvailable} onClick={() => void copyCommand()}>
                {copied ? <Check data-icon="inline-start" /> : <Copy data-icon="inline-start" />}
                {copied ? (zh ? "已复制" : "Copied") : (zh ? "复制命令" : "Copy command")}
              </Button>
            </CardAction>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {copyError ? <p role="status">{zh ? "无法自动复制，请选择下面的命令手动复制。" : "Could not copy automatically. Select and copy the command below."}</p> : null}
            <pre className="overflow-auto rounded-lg bg-muted p-3 font-mono text-sm"><code>{command || (zh ? "等待控制面返回真实 Token…" : "Waiting for Control to return the real token…")}</code></pre>
          </CardContent>
        </Card>
      )}

      {section === "maintenance" && <SystemUpdatePanel
        lang={lang}
        token={token}
        controlVersion={controlVersion}
        nodes={vm.nodes}
        onRefresh={onRefresh}
      />}

      {section === "network" && <div className="node-topology-split">
        <TrafficPaths lang={lang} paths={vm.trafficPaths} theme={theme} token={token} onRefresh={onRefresh} />
        <NodeTopology lang={lang} nodes={vm.nodes} services={vm.services} theme={theme} />
      </div>}
    </div>
  );
}
