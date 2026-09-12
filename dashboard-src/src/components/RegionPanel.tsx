import { useMemo, useState } from "react";
import { AlertCircle, Plus, Trash2 } from "lucide-react";
import { createRegion, removeRegion } from "../controlResourcesApi";
import { BUILTIN_REGIONS, regionChoices } from "../deploy/options";
import type { DashboardNode, DashboardRegion, Lang } from "../types";
import { useConfirm } from "./ConfirmDialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldDescription, FieldGroup, FieldLabel, FieldTitle } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

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
    <div className="flex flex-col gap-6">
      {element}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <Card>
          <CardHeader>
            <CardTitle>{zh ? "创建调度区域" : "Create a scheduling region"}</CardTitle>
            <CardDescription>
              {zh
                ? "节点加入时使用区域名，服务使用相同的 region 和副本数即可调度，无需指定机器。"
                : "Join nodes with the region name, then deploy with the same region and a replica count without pinning machines."}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <FieldGroup className="grid items-start gap-4 md:grid-cols-2">
              <Field data-disabled={Boolean(busy)}>
                <FieldLabel htmlFor="region-name">{zh ? "区域名称" : "Region name"}</FieldLabel>
                <Input id="region-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="batch-a" autoCapitalize="none" autoCorrect="off" spellCheck={false} disabled={Boolean(busy)} />
                <FieldDescription>{zh ? "保存时会自动转为小写。" : "Names are saved in lowercase."}</FieldDescription>
              </Field>
              <Field data-disabled={Boolean(busy)}>
                <FieldTitle id="region-egress-label">{zh ? "出网方式" : "Egress policy"}</FieldTitle>
                <ToggleGroup
                  aria-labelledby="region-egress-label"
                  aria-describedby="region-egress-description"
                  variant="outline"
                  value={[egress]}
                  disabled={Boolean(busy)}
                  onValueChange={(values) => { if (values[0]) setEgress(values[0] as "proxy" | "direct"); }}
                >
                  <ToggleGroupItem value="proxy">{zh ? "网关代理" : "Proxy"}</ToggleGroupItem>
                  <ToggleGroupItem value="direct">{zh ? "直接出网" : "Direct"}</ToggleGroupItem>
                </ToggleGroup>
                <FieldDescription id="region-egress-description">
                  {egress === "proxy" ? (zh ? "通过 manager 网关访问外网。" : "Access the internet through the manager gateway.") : (zh ? "直接访问外网，不经过网关。" : "Access the internet directly without a gateway.")}
                </FieldDescription>
              </Field>
            </FieldGroup>
          </CardContent>
          <CardFooter>
            <Button type="submit" disabled={Boolean(busy) || !name.trim()}>
              {busy === "create" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <Plus data-icon="inline-start" />}
              {busy === "create" ? (zh ? "创建中…" : "Creating…") : (zh ? "创建区域" : "Create region")}
            </Button>
          </CardFooter>
        </Card>
      </form>
      {error ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "区域操作失败" : "Region operation failed"}</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}
      <Card>
        <CardHeader>
          <CardTitle>{zh ? "区域列表" : "Regions"}</CardTitle>
          <CardDescription>{zh ? "内置区域只读；自定义区域没有节点时可以删除。" : "Built-in regions are read-only. Custom regions can be removed when no nodes are assigned."}</CardDescription>
        </CardHeader>
        <CardContent>
      <Table aria-label={zh ? "调度区域" : "Scheduling regions"} containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "区域列表" : "Region list" }}>
        <TableHeader>
          <TableRow>
            <TableHead>{zh ? "名称" : "Name"}</TableHead>
            <TableHead>{zh ? "类型" : "Kind"}</TableHead>
            <TableHead>egress</TableHead>
            <TableHead>{zh ? "节点" : "Nodes"}</TableHead>
            <TableHead>{zh ? "允许的入口" : "Exposures"}</TableHead>
            <TableHead><span className="sr-only">{zh ? "操作" : "Actions"}</span></TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((region) => (
            <TableRow key={region.name}>
              <TableCell>{region.name}</TableCell>
              <TableCell><Badge variant="secondary">{region.builtin ? (zh ? "内置" : "Built-in") : (zh ? "自定义" : "Custom")}</Badge></TableCell>
              <TableCell><Badge variant="outline">{region.egress || "-"}</Badge></TableCell>
              <TableCell>{region.nodeCount}</TableCell>
              <TableCell>
                <div className="flex flex-wrap gap-2">
                  {(region.exposures || (region.builtin ? [] : ["none"])).map((exposure) => <Badge key={exposure} variant="outline">{exposure}</Badge>)}
                  {region.builtin && !region.exposures?.length ? "-" : null}
                </div>
              </TableCell>
              <TableCell>
                {region.builtin ? null : (
                  <Button variant="destructive" size="sm" disabled={Boolean(busy)} aria-label={zh ? `删除区域 ${region.name}` : `Remove region ${region.name}`} onClick={() => void remove(region)}>
                    {busy === `remove:${region.name}` ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <Trash2 data-icon="inline-start" />}
                    {zh ? "删除" : "Remove"}
                  </Button>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
        </CardContent>
      </Card>
    </div>
  );
}
