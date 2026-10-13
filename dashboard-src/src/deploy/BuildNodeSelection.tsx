import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { apiPost } from "../apiClient";
import type { DashboardBuildNode, Lang } from "../types";

export function BuildNodeSelection({ token, lang, selected, candidates, onRefresh, onSaved }: {
  token: string; lang: Lang; selected: DashboardBuildNode[]; candidates: DashboardBuildNode[];
  onRefresh: () => Promise<void> | void;
  onSaved: (build: { nodes: DashboardBuildNode[]; configRevision: number }) => void;
}) {
  const zh = lang === "zh";
  const savedNames = selected.flatMap(node => node.name ? [node.name] : []).sort().join("\n");
  const [names, setNames] = useState(savedNames.split("\n").filter(Boolean));
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { if (!dirty) setNames(savedNames.split("\n").filter(Boolean)); }, [savedNames, dirty]);
  const choices = [...new Map([...candidates, ...selected].filter(node => node.name).map(node => [node.name!, node])).values()];
  const save = async () => {
    setSaving(true); setMessage(""); setError("");
    try {
      const response = await apiPost<{ build: { nodes: DashboardBuildNode[]; configRevision: number } }>("/v1/builds/config", token, { nodes: names });
      onSaved(response.build);
      setNames(response.build.nodes.flatMap(node => node.name ? [node.name] : []));
      setMessage(zh ? "构建节点名单已保存。已开始的构建继续执行。" : "Build-node list saved. Builds already started will continue.");
      setDirty(false);
      void Promise.resolve(onRefresh()).catch(() => {});
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setSaving(false); }
  };
  return <details className="rounded-lg border p-3">
    <summary className="cursor-pointer font-medium">{zh ? "配置构建节点" : "Configure build nodes"} · {selected.length}</summary>
    <p className="mt-3 text-sm text-muted-foreground">{zh
      ? "只有勾选的 Linux 节点可以接收新构建。amd64 与 arm64 分别原生构建；清空名单会暂停新任务分配。"
      : "Only selected Linux nodes may receive new builds. amd64 and arm64 build natively. Clearing the list pauses new assignments."}</p>
    <fieldset disabled={saving} className="my-3 flex flex-col gap-2">
      <legend className="sr-only">{zh ? "允许构建的节点" : "Allowed build nodes"}</legend>
      {choices.map(node => <label key={node.name} className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={names.includes(node.name!)} onChange={event => {
          setNames(current => event.target.checked ? [...current, node.name!] : current.filter(name => name !== node.name));
          setDirty(true); setMessage("");
        }} />
        <span>{node.name}</span><span className="text-muted-foreground">{node.platform || (zh ? "架构未知" : "Unknown architecture")}</span>
      </label>)}
      {!choices.length && <p className="text-sm">{zh ? "暂无已注册、架构已知的 Linux 节点。" : "No registered Linux nodes with known architecture."}</p>}
    </fieldset>
    <Button type="button" variant="outline" size="sm" disabled={saving || !dirty} onClick={() => void save()}>{saving ? (zh ? "保存中…" : "Saving…") : (zh ? "保存构建节点" : "Save build nodes")}</Button>
    {message && <p role="status" className="mt-2 text-sm">{message}</p>}
    {error && <p role="alert" className="mt-2 text-sm text-destructive">{error}</p>}
  </details>;
}
