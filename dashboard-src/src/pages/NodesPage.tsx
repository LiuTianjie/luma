import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { NodeFleetMap } from "../components/NodeFleetMap";
import { NodeTopology } from "../components/NodeTopology";
import { SystemUpdatePanel } from "../components/SystemUpdatePanel";
import { TrafficPaths } from "../components/TrafficPaths";
import { Badge } from "../components/ui";
import type { DashboardNode, Lang } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";

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
  const ready = vm.nodes.filter(readyNode).length;
  const managers = vm.nodes.filter(managerNode).length;
  const agents = vm.nodes.filter(agentReady).length;
  const terminalNodes = vm.nodes.filter((node) => node.terminalConnected).length;
  const regions = [...new Set(vm.nodes.map((node) => node.region).filter(Boolean))].sort();
  const command = joinCommand();
  const [copied, setCopied] = useState(false);

  const copyCommand = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard unavailable; ignore */
    }
  };

  return (
    <>
      <PageHeader
        meta={{
          eyebrow: zh ? "节点舰队" : "Fleet",
          title: zh ? "节点、Agent 与终端状态" : "Nodes, agents, and terminal readiness",
          description: zh
            ? "这里聚合已注册节点、Nomad 调度状态、节点 agent 心跳和终端可用性。"
            : "Inspect registered nodes, Nomad state, node-agent heartbeat, and terminal readiness.",
          metrics: [
            { label: zh ? "Ready 节点" : "Ready nodes", value: `${ready}/${vm.nodes.length}` },
            { label: zh ? "Ready Agent" : "Ready agents", value: `${agents}/${vm.nodes.length}` },
            { label: zh ? "Manager" : "Managers", value: managers },
            { label: "Terminal", value: terminalNodes },
          ],
        }}
      />

      <SystemUpdatePanel
        lang={lang}
        token={token}
        controlVersion={controlVersion}
        nodes={vm.nodes}
        onRefresh={onRefresh}
      />

      <section className="fleet-region-strip" aria-label={zh ? "区域" : "Regions"}>
        <Badge value="all" />
        {regions.map((region) => <Badge value={region || "-"} key={region} />)}
        {!regions.length ? <Badge value={zh ? "暂无区域" : "No regions"} /> : null}
      </section>

      <NodeFleetMap lang={lang} nodes={vm.nodes} services={vm.services} onSelect={onSelectNode} onTerminal={onTerminal} />

      <div className="node-topology-split">
        <TrafficPaths lang={lang} paths={vm.trafficPaths} theme={theme} token={token} onRefresh={onRefresh} />
        <NodeTopology lang={lang} nodes={vm.nodes} services={vm.services} theme={theme} />
      </div>

      <article className="panel fleet-command-panel">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">{zh ? "加入节点" : "Join node"}</p>
            <h2>{zh ? "在目标机器上执行" : "Run on the target host"}</h2>
          </div>
          <button type="button" className="ghost" onClick={() => void copyCommand()}>
            {copied ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}
            {copied ? (zh ? "已复制" : "Copied") : (zh ? "复制命令" : "Copy command")}
          </button>
        </div>
        <pre className="command-snippet"><code>{command}</code></pre>
        <p>
          {zh
            ? "控制域名已按当前访问地址填好。把 <node-join-token> 换成 luma node join token，<node-name> 换成节点名后在目标机器执行。"
            : "The control domain is filled from the current address. Replace <node-join-token> with a node join token and <node-name> with the node name, then run it on the target host."}
        </p>
      </article>
    </>
  );
}
