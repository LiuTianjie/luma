import "./InfrastructureWorkspace.css";
import { DetailDrawer } from "../DetailDrawer";
import { nodeDetail, serviceDetail } from "../detailRecords";
import { StatePill } from "../components/ui";
import { localizeState } from "../i18n";
import { useRouter, toHref } from "../router";
import { servicePath } from "../objectRoutes";
import type { DashboardNode, DashboardService, Lang } from "../types";

export function ResourceDetailPage({ lang, node, service, services, applicationNames, onTerminal }: {
  lang: Lang; node?: DashboardNode; service?: DashboardService; services: DashboardService[]; applicationNames: string[]; onTerminal: () => void;
}) {
  const { navigate } = useRouter();
  const zh = lang === "zh";
  const back = node ? "/fleet" : service?.stack && applicationNames.includes(service.stack) ? `/apps/${encodeURIComponent(service.stack)}/services` : "/apps";
  const detail = node ? nodeDetail(node) : service ? serviceDetail(service) : null;
  const nodeNames = new Set(node ? [node.name, node.hostname, node.displayName].filter((name): name is string => Boolean(name)) : []);
  const onNode = node ? services.filter(item => [item.node, ...(item.nodes || []), ...(item.tasks || []).map(task => task.node), ...(item.resources?.actual?.nodes || [])].some(name => Boolean(name) && nodeNames.has(name!))) : [];
  return <div className="detail-page infrastructure-workspace resource-workspace">
    <div className="page-actions"><a className="page-back" href={toHref(back)} onClick={e => { if (!e.metaKey && !e.ctrlKey) { e.preventDefault(); navigate(back); } }}>{zh ? "← 返回列表" : "← Back to list"}</a>
      <button type="button" disabled={node ? !node.terminalConnected : !service} onClick={onTerminal}>{zh ? "进入 Shell" : "Open shell"}</button></div>
    <DetailDrawer lang={lang} detail={detail} onClose={() => navigate(back)} inline showBack={false} />
    {node ? <section className="detail-section"><h2>{zh ? "运行服务" : "Workloads"}</h2><div className="table-wrap"><table><thead><tr><th>{zh ? "应用 / 服务" : "Application / service"}</th><th>{zh ? "状态" : "Status"}</th><th>{zh ? "副本" : "Replicas"}</th></tr></thead><tbody>
      {onNode.map(item => <tr key={item.fullName || item.name}><td><a href={toHref(servicePath(item.fullName || item.name || ""))} onClick={e => { if (!e.metaKey && !e.ctrlKey) { e.preventDefault(); navigate(servicePath(item.fullName || item.name || "")); } }}>{item.stack ? `${item.stack} / ` : ""}{item.name}</a></td><td><StatePill value={item.status || item.health || "unknown"} label={localizeState(lang, item.status || item.health || "unknown")} /></td><td>{item.running ?? 0}/{item.desired ?? 0}</td></tr>)}
      {!onNode.length ? <tr><td colSpan={3}>{zh ? "当前没有关联的服务" : "No associated services"}</td></tr> : null}
    </tbody></table></div></section> : null}
  </div>;
}
