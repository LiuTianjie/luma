import { useState } from "react";
import { Check, Copy } from "lucide-react";
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

// Fill the control domain from where the dashboard is served. The join token stays a
// placeholder — it is never rendered from state; run `luma node join` on the host.
function joinCommand() {
  const origin = typeof window !== "undefined" ? window.location.origin : "https://<control-domain>";
  return `luma node join ${origin} --token <node-join-token> --region home --name <node-name>`;
}

export function NodesPage({
  lang,
  vm,
  theme,
  token,
  onSelectNode,
  onTerminal,
  onRefresh,
  controlVersion,
}: {
  lang: Lang;
  vm: DashboardViewModel;
  theme: "light" | "dark";
  token: string;
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
  const command = joinCommand();
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

      {section !== "network" && <nav className="workspace-tabs" aria-label={zh ? "节点管理" : "Node management"}>
        {[
          ["nodes", "/fleet", zh ? "节点列表" : "All nodes"],
          ["join", "/fleet/join", zh ? "加入节点" : "Join node"],
          ["regions", "/fleet/regions", zh ? "区域" : "Regions"],
          ["maintenance", "/fleet/maintenance", zh ? "系统维护" : "Maintenance"],
        ].map(([key, href, label]) => <a key={key} href={toHref(href)} className={section === key ? "active" : ""} aria-current={section === key ? "page" : undefined} onClick={(event) => {
          if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
          event.preventDefault(); navigate(href);
        }}>{label}</a>)}
      </nav>}

      {section === "unknown" && <div className="empty-inline"><p>{zh ? "此基础设施页面不存在。" : "This infrastructure page does not exist."}</p><button type="button" onClick={() => navigate("/fleet")}>{zh ? "返回节点列表" : "Back to nodes"}</button></div>}
      {section === "nodes" && <NodeFleetMap lang={lang} nodes={vm.nodes} services={vm.services} onSelect={onSelectNode} onTerminal={onTerminal} />}
      {section === "regions" && <RegionPanel lang={lang} token={token} regions={vm.regions} nodes={vm.nodes} onRefresh={onRefresh} />}

      {section === "join" && <article className="panel fleet-command-panel">
        <div className="panel-heading">
          <div>
            <h2>{zh ? "在目标机器上执行" : "Run on the target host"}</h2>
          </div>
          <button type="button" className="ghost" onClick={() => void copyCommand()}>
            {copied ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}
            {copied ? (zh ? "已复制" : "Copied") : (zh ? "复制命令" : "Copy command")}
          </button>
        </div>
        {copyError && <p role="status">{zh ? "无法自动复制，请选择下面的命令手动复制。" : "Could not copy automatically. Select and copy the command below."}</p>}
        <pre className="command-snippet"><code>{command}</code></pre>
        <p>
          {zh
            ? "控制域名已按当前访问地址填好。把 <node-join-token> 换成 luma node join token，--region 换成区域管理中创建的 Region 名，<node-name> 换成节点名后在目标机器执行。"
            : "The control domain is filled from the current address. Replace <node-join-token> with a node join token, --region with a created region name, and <node-name> with the node name, then run it on the target host."}
        </p>
      </article>}

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
