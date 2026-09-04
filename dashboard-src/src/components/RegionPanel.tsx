import { useMemo, useState } from "react";
import { createRegion, removeRegion } from "../controlResourcesApi";
import { BUILTIN_REGIONS, regionChoices } from "../deploy/options";
import type { DashboardNode, DashboardRegion, Lang } from "../types";
import { useConfirm } from "./ConfirmDialog";
import { Badge, BadgeGroup, PrimaryCell } from "./ui";

export function RegionPanel({
  lang,
  token,
  regions,
  nodes,
  onRefresh,
}: {
  lang: Lang;
  token: string;
  regions: DashboardRegion[];
  nodes: DashboardNode[];
  onRefresh: () => Promise<void> | void;
}) {
  const zh = lang === "zh";
  const { confirm, element } = useConfirm(lang);
  const [name, setName] = useState("");
  const [egress, setEgress] = useState<"proxy" | "direct">("proxy");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const rows = useMemo(() => {
    const catalog = new Map<string, DashboardRegion>();
    for (const region of regions) {
      if (region.name) catalog.set(region.name, region);
    }
    for (const builtin of BUILTIN_REGIONS) {
      if (!catalog.has(builtin)) catalog.set(builtin, { name: builtin, builtin: true, egress: builtin === "global" ? "direct" : "proxy" });
    }
    for (const extra of regionChoices(regions, nodes)) {
      if (!catalog.has(extra)) catalog.set(extra, { name: extra, builtin: BUILTIN_REGIONS.includes(extra) });
    }
    const counts = new Map<string, number>();
    for (const node of nodes) {
      const region = node.region || "";
      if (!region) continue;
      counts.set(region, (counts.get(region) || 0) + 1);
    }
    return [...catalog.values()].map((region) => ({
      ...region,
      nodeCount: counts.get(region.name) || 0,
    }));
  }, [nodes, regions]);

  const submit = async () => {
    const trimmed = name.trim().toLowerCase();
    if (!trimmed) return;
    setBusy("create");
    setError("");
    try {
      await createRegion({ token, name: trimmed, egress });
      setName("");
      await onRefresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy("");
    }
  };

  const remove = async (region: DashboardRegion) => {
    if (region.builtin) return;
    const ok = await confirm({
      title: zh ? `删除 Region ${region.name}？` : `Remove region ${region.name}?`,
      body: zh
        ? <p>只删除空的自定义 Region。节点还在这个 Region 里时不能删。</p>
        : <p>Only unused custom regions can be removed. Nodes still assigned here will block deletion.</p>,
      confirmLabel: zh ? "删除" : "Remove",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(`remove:${region.name}`);
    setError("");
    try {
      await removeRegion({ token, name: region.name });
      await onRefresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy("");
    }
  };

  return (
    <article className="panel region-panel">
      {element}
      <div className="panel-heading">
        <div>
          <p className="eyebrow">{zh ? "调度区域" : "Regions"}</p>
          <h2>{zh ? "给一批机器单独建 Region" : "Create a region for a machine pool"}</h2>
        </div>
        <span>{rows.length}</span>
      </div>
      <p className="region-panel-copy">
        {zh
          ? "自定义 Region 只用于调度。节点 join 时带上这个名字，服务 YAML 写同样的 region 和 replicas 即可，不必指定具体机器。"
          : "Custom regions are scheduling pools. Join nodes with this name, then deploy with the same region and a replica count. You do not pin individual machines."}
      </p>
      <form
        className="credential-form region-create-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <label>
          <span>{zh ? "Region 名" : "Region name"}</span>
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="batch-a" autoCapitalize="none" autoCorrect="off" spellCheck={false} />
        </label>
        <label>
          <span>egress</span>
          <select value={egress} onChange={(event) => setEgress(event.target.value as "proxy" | "direct")}>
            <option value="proxy">{zh ? "proxy（走 manager 网关）" : "proxy (manager gateway)"}</option>
            <option value="direct">{zh ? "direct（直连外网）" : "direct (no gateway)"}</option>
          </select>
        </label>
        <button type="submit" className="primary" disabled={busy === "create" || !name.trim()}>
          {busy === "create" ? (zh ? "创建中..." : "Creating...") : (zh ? "创建 Region" : "Create region")}
        </button>
      </form>
      {error ? <div className="alert alert-warning"><span>{error}</span></div> : null}
      <div className="table-wrap">
        <table className="storage-table">
          <thead>
            <tr>
              <th>{zh ? "名称" : "Name"}</th>
              <th>{zh ? "类型" : "Kind"}</th>
              <th>egress</th>
              <th>{zh ? "节点" : "Nodes"}</th>
              <th>{zh ? "允许的入口" : "Exposures"}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((region) => (
              <tr key={region.name}>
                <td><PrimaryCell title={region.name} /></td>
                <td><Badge value={region.builtin ? "builtin" : "custom"} /></td>
                <td><Badge value={region.egress || "-"} /></td>
                <td><Badge value={String(region.nodeCount)} /></td>
                <td>
                  <BadgeGroup>
                    {(region.exposures || (region.builtin ? [] : ["none"])).map((exposure) => <Badge key={exposure} value={exposure} />)}
                  </BadgeGroup>
                </td>
                <td>
                  {region.builtin ? null : (
                    <button type="button" className="ghost" disabled={busy === `remove:${region.name}`} onClick={() => void remove(region)}>
                      {zh ? "删除" : "Remove"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </article>
  );
}
