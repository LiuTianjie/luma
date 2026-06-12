import { localizeState, t } from "../i18n";
import type { DashboardNode, Lang } from "../types";
import { Badge, PrimaryCell, StatePill } from "./ui";

export function NodesTable({
  lang,
  nodes,
  onSelect,
  onTerminal,
}: {
  lang: Lang;
  nodes: DashboardNode[];
  onSelect: (node: DashboardNode) => void;
  onTerminal?: (node: DashboardNode) => void;
}) {
  return (
    <article className="panel nodes-panel" id="section-2">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">{t(lang, "nodesEyebrow")}</p>
          <h2>{t(lang, "nodes")}</h2>
        </div>
        <span>{nodes.length}</span>
      </div>
      <div className="table-wrap">
        <table className="nodes-table">
          <colgroup>
            <col className="node-name-col" />
            <col className="node-region-col" />
            <col className="node-role-col" />
            <col className="node-state-col" />
            <col className="node-agent-col" />
            <col className="node-availability-col" />
            <col className="node-leader-col" />
            <col className="node-actions-col" />
          </colgroup>
          <thead>
            <tr>
              <th>{t(lang, "name")}</th>
              <th>{t(lang, "region")}</th>
              <th>{t(lang, "role")}</th>
              <th>{t(lang, "state")}</th>
              <th>Agent</th>
              <th>{t(lang, "availability")}</th>
              <th>{t(lang, "leader")}</th>
              <th>{t(lang, "actions")}</th>
            </tr>
          </thead>
          <tbody>
            {nodes.map((node, index) => {
              const terminalReady = (node.agentStatus || "").toLowerCase() === "ready" && (node.storageCapabilities || []).includes("terminal");
              return (
                <tr key={`${node.name || "node"}-${index}`} onClick={() => onSelect(node)}>
                  <td><PrimaryCell meta={node.displayName} title={node.name || "-"} /></td>
                  <td><Badge value={node.region || "-"} /></td>
                  <td><Badge value={node.role || "-"} /></td>
                  <td><StatePill label={localizeState(lang, node.state)} value={node.state} /></td>
                  <td><Badge value={`${node.agentStatus || "missing"}${node.agentOs ? ` / ${node.agentOs}` : ""}`} /></td>
                  <td><Badge value={node.availability || "-"} /></td>
                  <td>{node.leader ? <Badge value={t(lang, "yes")} /> : "-"}</td>
                  <td>
                    <button
                      type="button"
                      className="table-action-button"
                      disabled={!terminalReady || !onTerminal}
                      onClick={(event) => {
                        event.stopPropagation();
                        onTerminal?.(node);
                      }}
                    >
                      Terminal
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </article>
  );
}
