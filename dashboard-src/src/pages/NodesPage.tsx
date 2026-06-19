import { Copy, Server, TerminalSquare, Wrench } from "lucide-react";
import { NodeFleetMap } from "../components/NodeFleetMap";
import { NodesTable } from "../components/NodesTable";
import { Badge } from "../components/ui";
import { t } from "../i18n";
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

function joinCommand(lang: Lang) {
  return lang === "zh"
    ? "luma node join https://<control-domain> --token <node-join-token> --region home --name <node-name>"
    : "luma node join https://<control-domain> --token <node-join-token> --region home --name <node-name>";
}

export function NodesPage({
  lang,
  vm,
  onSelectNode,
  onTerminal,
}: {
  lang: Lang;
  vm: DashboardViewModel;
  onSelectNode: (node: DashboardNode) => void;
  onTerminal: (node: DashboardNode) => void;
}) {
  const zh = lang === "zh";
  const ready = vm.nodes.filter(readyNode).length;
  const managers = vm.nodes.filter(managerNode).length;
  const agents = vm.nodes.filter(agentReady).length;
  const terminalNodes = vm.nodes.filter((node) => node.terminalConnected).length;
  const regions = [...new Set(vm.nodes.map((node) => node.region).filter(Boolean))].sort();

  return (
    <>
      <PageHeader
        meta={{
          eyebrow: zh ? "节点舰队" : "Fleet",
          title: zh ? "节点、Agent 与终端状态" : "Nodes, agents, and terminal readiness",
          description: zh
            ? "这里聚合已注册节点、Nomad 调度状态、节点 agent 心跳和终端可用性。危险写操作先保留为命令提示。"
            : "Inspect registered nodes, Nomad state, node-agent heartbeat, and terminal readiness. Mutating fleet actions stay as command hints for now.",
          metrics: [
            { label: zh ? "Ready 节点" : "Ready nodes", value: `${ready}/${vm.nodes.length}` },
            { label: zh ? "Ready Agent" : "Ready agents", value: `${agents}/${vm.nodes.length}` },
            { label: zh ? "Manager" : "Managers", value: managers },
            { label: "Terminal", value: terminalNodes },
          ],
          action: (
            <button className="ghost" type="button" disabled title={zh ? "当前 UI 仅展示命令，节点加入仍需在目标机器执行 CLI。" : "The UI only shows the command. Node join still runs on the target host."}>
              <Copy size={16} aria-hidden="true" />
              {zh ? "加入命令" : "Join command"}
            </button>
          ),
        }}
      />

      <section className="fleet-region-strip" aria-label={zh ? "区域" : "Regions"}>
        <Badge value="all" />
        {regions.map((region) => <Badge value={region || "-"} key={region} />)}
        {!regions.length ? <Badge value={zh ? "暂无区域" : "No regions"} /> : null}
      </section>

      <NodeFleetMap lang={lang} nodes={vm.nodes} services={vm.services} onSelect={onSelectNode} onTerminal={onTerminal} />

      <section className="fleet-ops-grid">
        <article className="panel fleet-command-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">{zh ? "加入节点" : "Join node"}</p>
              <h2>{zh ? "在目标机器上执行" : "Run on the target host"}</h2>
            </div>
            <Server size={18} aria-hidden="true" />
          </div>
          <pre className="command-snippet"><code>{joinCommand(lang)}</code></pre>
          <p>{zh ? "节点加入、退出、重装 agent 都会改主机状态，所以本轮 UI 先不直接触发。" : "Join, exit, and agent reinstall mutate host state, so this UI does not trigger them directly yet."}</p>
        </article>
        <article className="panel fleet-action-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">{zh ? "受保护操作" : "Guarded actions"}</p>
              <h2>{zh ? "展示但暂不执行" : "Visible, not executable"}</h2>
            </div>
            <Wrench size={18} aria-hidden="true" />
          </div>
          <div className="disabled-action-list">
            <button type="button" disabled><Wrench size={15} aria-hidden="true" />{zh ? "Fleet update" : "Fleet update"}</button>
            <button type="button" disabled><TerminalSquare size={15} aria-hidden="true" />{zh ? "重装 Agent" : "Refresh agent"}</button>
            <button type="button" disabled>{zh ? "Drain 节点" : "Drain node"}</button>
            <button type="button" disabled>{zh ? "移除节点" : "Remove node"}</button>
          </div>
          <p>{zh ? "这些 API 在控制面存在，但需要确认权限、危险确认和回滚语义后再开放前端写入。" : "The control-plane APIs exist, but the frontend write flow needs permissions, confirmations, and rollback semantics before it is enabled."}</p>
        </article>
      </section>

      <NodesTable lang={lang} nodes={vm.nodes} onSelect={onSelectNode} onTerminal={onTerminal} />
    </>
  );
}
