import { useState } from "react";
import { Check, CircleAlert, Globe } from "@/components/icons";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { useConfirm } from "./ConfirmDialog";
import { setNodePublicIngress } from "../dnsApi";
import { formatDateTime } from "../format";
import type { DashboardNode, Lang, NodePublicIngress as Ingress } from "../types";

function isManager(node: DashboardNode): boolean {
  return ["server", "manager"].includes(node.role || "") || Boolean(node.leader);
}

/** Lets a node with a public address serve its own HTTP traffic (exposure: node-edge). */
export function NodePublicIngress({ lang, token, node, onChanged }: { lang: Lang; token: string; node: DashboardNode; onChanged?: () => Promise<void> | void }) {
  const zh = lang === "zh";
  const { confirm, element } = useConfirm(lang);
  const [override, setOverride] = useState<Ingress | null>(null);
  const ingress = override || node.publicIngress || { enabled: false };
  const [address, setAddress] = useState(ingress.publicIp || node.publicIp || "");
  const [busy, setBusy] = useState<"" | "enable" | "disable">("");
  const [error, setError] = useState("");
  const name = node.name || "";
  const macos = (node.agentOs || "").toLowerCase() === "darwin";
  const manager = isManager(node);
  const unsupported = manager
    ? (zh ? "Manager 已经是集群入口，它上面的服务直接用 cn-edge。" : "The Manager is already the cluster edge; its services use cn-edge.")
    : macos
      ? (zh ? "macOS 节点不能做公网入口，请用有公网 IP 的 Linux 节点。" : "macOS nodes cannot serve public ingress; use a Linux node with a public IP.")
      : "";

  const enable = async () => {
    setBusy("enable");
    setError("");
    try {
      const result = await setNodePublicIngress({ token, node: name, enabled: true, publicIp: address.trim() });
      setOverride(result.publicIngress || null);
      await onChanged?.();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy("");
    }
  };

  const disable = async () => {
    const ok = await confirm({
      title: zh ? `关闭 ${name} 的公网入口？` : `Disable public ingress on ${name}?`,
      body: zh
        ? <p>会停止这台节点上的 Traefik。仍有 node-edge 服务指向它时，Control 会拒绝关闭。</p>
        : <p>This stops the Traefik on this node. Control refuses while a node-edge service still uses it.</p>,
      confirmLabel: zh ? "关闭" : "Disable",
      tone: "danger",
    });
    if (!ok) return;
    setBusy("disable");
    setError("");
    try {
      await setNodePublicIngress({ token, node: name, enabled: false });
      setOverride({ enabled: false });
      await onChanged?.();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy("");
    }
  };

  const status = ingress.enabled ? (ingress.status === "ready" ? "ready" : "unreachable") : "off";

  return (
    <Card>
      {element}
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><Globe aria-hidden="true" />{zh ? "公网入口" : "Public ingress"}</CardTitle>
        <CardDescription>
          {zh
            ? "开启后这台节点自己运行 Traefik 接收 80/443。部署时选 node-edge 并指定这台节点，域名直接解析到它的公网 IP，流量不经过 Manager。"
            : "This node runs its own Traefik on 80/443. Services deployed with node-edge on this node resolve straight to its public IP, so traffic never crosses the Manager."}
        </CardDescription>
        <CardAction>
          {status === "ready" ? <Badge>{zh ? "已开启" : "Enabled"}</Badge>
            : status === "unreachable" ? <Badge variant="destructive">{zh ? "端口不可达" : "Ports unreachable"}</Badge>
              : <Badge variant="outline">{zh ? "未开启" : "Off"}</Badge>}
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {unsupported ? <p className="text-sm text-muted-foreground">{unsupported}</p> : (
          <Field data-disabled={Boolean(busy)} className="max-w-md">
            <FieldLabel htmlFor={`public-ip-${name}`}>{zh ? "公网 IP" : "Public IP"}</FieldLabel>
            <Input id={`public-ip-${name}`} value={address} onChange={(event) => setAddress(event.target.value)} placeholder="47.100.1.20" inputMode="decimal" autoComplete="off" spellCheck={false} disabled={Boolean(busy)} />
            <FieldDescription>
              {zh
                ? "填客户端从公网访问的地址。云服务器安全组要放行 TCP 80/443；国内节点的域名需要在该云厂商完成备案接入。"
                : "The address clients reach from the internet. Allow TCP 80/443 in the cloud security group; in mainland China the domain needs ICP filing at that provider."}
            </FieldDescription>
          </Field>
        )}
        {ingress.enabled && ingress.detail ? (
          <Alert variant={status === "ready" ? "default" : "destructive"}>
            {status === "ready" ? <Check /> : <CircleAlert />}
            <AlertTitle>{status === "ready" ? (zh ? "外部可达" : "Reachable") : (zh ? "从 Manager 探测不到端口" : "Ports did not answer from the Manager")}</AlertTitle>
            <AlertDescription>{ingress.detail}{ingress.updatedAt ? ` · ${formatDateTime(ingress.updatedAt, lang)}` : ""}</AlertDescription>
          </Alert>
        ) : null}
        {error ? <Alert variant="destructive"><CircleAlert /><AlertTitle>{zh ? "操作失败" : "Operation failed"}</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}
      </CardContent>
      {unsupported ? null : (
        <CardFooter className="flex flex-wrap gap-2">
          <Button type="button" disabled={Boolean(busy) || !address.trim() || node.agentStatus !== "ready"} onClick={() => void enable()}>
            {busy === "enable" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}
            {busy === "enable"
              ? (zh ? "部署 Traefik 并检查端口…" : "Deploying Traefik and checking ports…")
              : ingress.enabled ? (zh ? "重新检查 / 更新地址" : "Re-check / update address") : (zh ? "开启公网入口" : "Enable public ingress")}
          </Button>
          {ingress.enabled ? (
            <Button type="button" variant="outline" disabled={Boolean(busy)} onClick={() => void disable()}>
              {busy === "disable" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}
              {zh ? "关闭" : "Disable"}
            </Button>
          ) : null}
          {node.agentStatus !== "ready" ? <span className="text-sm text-muted-foreground">{zh ? "节点 Agent 在线后才能开启。" : "Available once the node agent is online."}</span> : null}
        </CardFooter>
      )}
    </Card>
  );
}
