import { localizeState, t } from "../i18n";
import type { DashboardNode, Lang } from "../types";
import { Badge, PrimaryCell, StatePill } from "./ui";

export function NodesTable({
  lang,
  nodes,
  onSelect,
}: {
  lang: Lang;
  nodes: DashboardNode[];
  onSelect: (node: DashboardNode) => void;
}) {
  return (
    <article className="panel nodes-panel" id="section-1">
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
            <col className="node-availability-col" />
            <col className="node-leader-col" />
          </colgroup>
          <thead>
            <tr>
              <th>{t(lang, "name")}</th>
              <th>{t(lang, "region")}</th>
              <th>{t(lang, "role")}</th>
              <th>{t(lang, "state")}</th>
              <th>{t(lang, "availability")}</th>
              <th>{t(lang, "leader")}</th>
            </tr>
          </thead>
          <tbody>
            {nodes.map((node, index) => (
              <tr key={`${node.name || "node"}-${index}`} onClick={() => onSelect(node)}>
                <td><PrimaryCell meta={node.displayName} title={node.name || "-"} /></td>
                <td><Badge value={node.region || "-"} /></td>
                <td><Badge value={node.role || "-"} /></td>
                <td><StatePill label={localizeState(lang, node.state)} value={node.state} /></td>
                <td><Badge value={node.availability || "-"} /></td>
                <td>{node.leader ? <Badge value={t(lang, "yes")} /> : "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </article>
  );
}
