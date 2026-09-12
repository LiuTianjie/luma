import { useEffect, useMemo, useState, type FormEvent } from "react";
import { AlertCircle, CheckCircle2, Cloud, KeyRound, Network, RefreshCw, ShieldCheck } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldDescription, FieldGroup, FieldLabel, FieldLegend, FieldSet } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { configureSetup, runSetupChecks, setBuildConfig, type SetupCheckPayload } from "../setupApi";
import { setSecret } from "../controlResourcesApi";
import { PageHeader } from "./PageHeader";
import type { Lang, Readiness } from "../types";

type SetupForm = {
  cloudflareToken: string;
  cloudflareZone: string;
  cloudflareZoneId: string;
  edgeTarget: string;
  tailscaleAuthKey: string;
  acmeEmail: string;
  egressSubscriptionUrl: string;
  registryHost: string;
  pushHost: string;
};

const emptyForm: SetupForm = {
  cloudflareToken: "",
  cloudflareZone: "",
  cloudflareZoneId: "",
  edgeTarget: "",
  tailscaleAuthKey: "",
  acmeEmail: "",
  egressSubscriptionUrl: "",
  registryHost: "",
  pushHost: "",
};

const integrationDefinitions = [
  { id: "cloudflare", zh: "Cloudflare", en: "Cloudflare", icon: Cloud },
  { id: "tailscale", zh: "Tailscale", en: "Tailscale", icon: Network },
  { id: "acme", zh: "ACME / TLS", en: "ACME / TLS", icon: ShieldCheck },
  { id: "egress", zh: "出网代理", en: "Egress", icon: Network },
  { id: "registry", zh: "构建镜像仓库", en: "Builder registry", icon: KeyRound },
] as const;

function statusFor(item: { configured?: boolean; required?: boolean } | undefined) {
  if (item?.configured) return { variant: "success" as const, zh: "已配置", en: "Ready" };
  if (item?.required) return { variant: "destructive" as const, zh: "需要处理", en: "Action needed" };
  return { variant: "secondary" as const, zh: "可选", en: "Optional" };
}

function checkVariant(status?: string) {
  if (status === "ready") return "success" as const;
  if (status === "missing" || status === "error") return "destructive" as const;
  if (status === "skipped") return "secondary" as const;
  return "warning" as const;
}

export function SetupPage({ lang, token, readiness, onRefresh }: { lang: Lang; token: string; readiness?: Readiness; onRefresh: () => Promise<void> | void }) {
  const zh = lang === "zh";
  const [form, setForm] = useState<SetupForm>(() => ({
    ...emptyForm,
    cloudflareZone: readiness?.dns?.zone || "",
    edgeTarget: readiness?.dns?.target || "",
  }));
  const [checks, setChecks] = useState<SetupCheckPayload | null>(readiness?.setupLastCheck || null);
  const [busy, setBusy] = useState<"save" | "check" | "">("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    setChecks(readiness?.setupLastCheck || null);
    setForm((current) => ({
      ...current,
      cloudflareZone: readiness?.dns?.zone || current.cloudflareZone,
      edgeTarget: readiness?.dns?.target || current.edgeTarget,
    }));
  }, [readiness]);

  const setupReadiness = readiness?.setup || {};
  const configuredCount = useMemo(() => integrationDefinitions.filter(({ id }) => setupReadiness[id]?.configured).length, [setupReadiness]);

  const update = (key: keyof SetupForm, value: string) => setForm((current) => ({ ...current, [key]: value }));

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const values = [
      ["CLOUDFLARE_API_TOKEN", form.cloudflareToken],
      ["CLOUDFLARE_ZONE_ID", form.cloudflareZoneId],
      ["TAILSCALE_AUTHKEY", form.tailscaleAuthKey],
      ["TRAEFIK_ACME_EMAIL", form.acmeEmail],
      ["EGRESS_SUBSCRIPTION_URL", form.egressSubscriptionUrl],
    ] as const;
    const pending = values.filter(([, value]) => value.trim());
    const hasRegistry = Boolean(form.registryHost.trim() || form.pushHost.trim());
    const hasDnsConfig = Boolean(form.cloudflareZone.trim() || form.cloudflareZoneId.trim() || form.edgeTarget.trim());
    if (!pending.length && !hasRegistry && !hasDnsConfig) {
      setError(zh ? "请至少填写一项要保存的配置。" : "Enter at least one value to save.");
      return;
    }
    if (hasRegistry && (!form.registryHost.trim() || !form.pushHost.trim())) {
      setError(zh ? "registryHost 和 pushHost 需要同时填写。" : "registryHost and pushHost must be provided together.");
      return;
    }
    setBusy("save");
    setError("");
    setNotice("");
    try {
      if (hasDnsConfig) {
        await configureSetup(token, {
          cloudflareZone: form.cloudflareZone.trim(),
          cloudflareZoneId: form.cloudflareZoneId.trim(),
          edgeTarget: form.edgeTarget.trim(),
        });
      }
      for (const [name, value] of pending) await setSecret({ token, name, value: value.trim() });
      if (hasRegistry) await setBuildConfig(token, { registryHost: form.registryHost.trim(), pushHost: form.pushHost.trim() });
      setForm((current) => ({ ...emptyForm, cloudflareZone: current.cloudflareZone, edgeTarget: current.edgeTarget }));
      setNotice(zh ? "配置已保存，正在刷新集群状态。" : "Configuration saved; refreshing cluster readiness.");
      await onRefresh();
    } catch (cause) {
      setError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      setBusy("");
    }
  };

  const verify = async () => {
    setBusy("check");
    setError("");
    try {
      setChecks(await runSetupChecks(token));
      setNotice(zh ? "依赖验证已完成。" : "Dependency verification completed.");
      await onRefresh();
    } catch (cause) {
      setError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <PageHeader meta={{
        eyebrow: zh ? "平台初始化" : "Platform setup",
        title: zh ? "首次安装" : "First install",
        description: zh ? "配置 Luma 运行所需的外部依赖，然后验证控制面与节点是否可以工作。" : "Configure the external dependencies Luma needs, then verify that Control and the nodes are ready.",
        metrics: [{ label: zh ? "已就绪" : "Ready", value: `${configuredCount}/${integrationDefinitions.length}` }],
      }} />

      {error ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "保存或验证失败" : "Save or verification failed"}</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}
      {notice ? <Alert><CheckCircle2 /><AlertDescription>{notice}</AlertDescription></Alert> : null}

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5" aria-label={zh ? "依赖状态" : "Dependency status"}>
        {integrationDefinitions.map(({ id, zh: labelZh, en: labelEn, icon: Icon }) => {
          const status = statusFor(setupReadiness[id]);
          return <Card key={id} size="sm"><CardContent className="flex items-center gap-3 py-3"><Icon className="size-4 text-muted-foreground" /><div className="min-w-0 flex-1"><p className="truncate font-medium">{zh ? labelZh : labelEn}</p><p className="text-xs text-muted-foreground">{setupReadiness[id]?.configured ? (zh ? "可供控制面使用" : "Available to Control") : (zh ? "尚未完成" : "Not configured")}</p></div><Badge variant={status.variant}>{zh ? status.zh : status.en}</Badge></CardContent></Card>;
        })}
      </section>

      <form className="grid gap-4 xl:grid-cols-2" onSubmit={submit}>
        <Card>
          <CardHeader><CardTitle>Cloudflare</CardTitle><CardDescription>{zh ? "用于 DNS、边缘入口和证书自动化。Token 只写入，不会回显。" : "Used for DNS, edge routes, and certificate automation. The token is write-only."}</CardDescription></CardHeader>
          <CardContent><FieldSet><FieldLegend>{zh ? "边缘与 DNS" : "Edge and DNS"}</FieldLegend><FieldDescription>{zh ? "Zone 与边缘目标会写入控制面配置。" : "The zone and edge target are saved to Control configuration."}</FieldDescription><FieldGroup>
            <Field><FieldLabel htmlFor="cloudflare-token">API Token</FieldLabel><Input id="cloudflare-token" type="password" autoComplete="new-password" placeholder="CLOUDFLARE_API_TOKEN" value={form.cloudflareToken} onChange={(event) => update("cloudflareToken", event.target.value)} /></Field>
            <Field><FieldLabel htmlFor="cloudflare-zone">Zone</FieldLabel><Input id="cloudflare-zone" placeholder={zh ? "例如 example.com" : "e.g. example.com"} value={form.cloudflareZone} onChange={(event) => update("cloudflareZone", event.target.value)} /></Field>
            <Field><FieldLabel htmlFor="cloudflare-zone-id">Zone ID</FieldLabel><Input id="cloudflare-zone-id" placeholder="CLOUDFLARE_ZONE_ID" value={form.cloudflareZoneId} onChange={(event) => update("cloudflareZoneId", event.target.value)} /></Field>
            <Field><FieldLabel htmlFor="edge-target">Edge target</FieldLabel><Input id="edge-target" placeholder={zh ? "例如 203.0.113.10" : "e.g. 203.0.113.10"} value={form.edgeTarget} onChange={(event) => update("edgeTarget", event.target.value)} /></Field>
          </FieldGroup></FieldSet></CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>{zh ? "节点与证书" : "Nodes and certificates"}</CardTitle><CardDescription>{zh ? "跨网络接入和 HTTPS 自动签发使用这些配置。" : "These values enable cross-network joins and automated HTTPS certificates."}</CardDescription></CardHeader>
          <CardContent><FieldGroup>
            <Field><FieldLabel htmlFor="tailscale-authkey">Tailscale auth key</FieldLabel><Input id="tailscale-authkey" type="password" autoComplete="new-password" placeholder="TAILSCALE_AUTHKEY" value={form.tailscaleAuthKey} onChange={(event) => update("tailscaleAuthKey", event.target.value)} /><FieldDescription>{zh ? "用于跨网络节点接入和内部服务互联。" : "Used for cross-network node joins and private service connectivity."}</FieldDescription></Field>
            <Field><FieldLabel htmlFor="acme-email">ACME email</FieldLabel><Input id="acme-email" type="email" placeholder="TRAEFIK_ACME_EMAIL" value={form.acmeEmail} onChange={(event) => update("acmeEmail", event.target.value)} /><FieldDescription>{zh ? "用于 Traefik 自动签发 HTTPS 证书。" : "Used by Traefik to issue HTTPS certificates."}</FieldDescription></Field>
          </FieldGroup></CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>{zh ? "可选出网" : "Optional egress"}</CardTitle><CardDescription>{zh ? "需要代理出网或大陆节点拉取镜像时填写。" : "Use this when workloads or mainland nodes need proxy egress."}</CardDescription></CardHeader>
          <CardContent><Field><FieldLabel htmlFor="egress-subscription">Subscription URL</FieldLabel><Input id="egress-subscription" type="url" placeholder="EGRESS_SUBSCRIPTION_URL" value={form.egressSubscriptionUrl} onChange={(event) => update("egressSubscriptionUrl", event.target.value)} /></Field></CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>{zh ? "Builder 镜像仓库" : "Builder registry"}</CardTitle><CardDescription>{zh ? "构建节点必须同时知道拉取地址和推送地址。" : "Builder nodes need both a pull address and a push address."}</CardDescription></CardHeader>
          <CardContent><FieldGroup>
            <Field><FieldLabel htmlFor="registry-host">registryHost</FieldLabel><Input id="registry-host" placeholder="100.64.0.10:5000" value={form.registryHost} onChange={(event) => update("registryHost", event.target.value)} /></Field>
            <Field><FieldLabel htmlFor="push-host">pushHost</FieldLabel><Input id="push-host" placeholder="100.64.0.10:5000" value={form.pushHost} onChange={(event) => update("pushHost", event.target.value)} /></Field>
          </FieldGroup></CardContent>
        </Card>

        <Card className="xl:col-span-2">
          <CardContent className="flex flex-wrap gap-3">
            <Button type="submit" disabled={busy !== ""}>{busy === "save" ? (zh ? "保存中…" : "Saving…") : (zh ? "保存配置" : "Save configuration")}</Button>
            <Button type="button" variant="outline" disabled={busy !== ""} onClick={() => void verify()}><RefreshCw data-icon="inline-start" />{busy === "check" ? (zh ? "验证中…" : "Checking…") : (zh ? "立即验证依赖" : "Verify dependencies")}</Button>
            <p className="basis-full text-xs text-muted-foreground">{zh ? "空白字段不会覆盖已有 Secret；敏感值只写入不回显。" : "Blank fields do not overwrite existing secrets; sensitive values are write-only."}</p>
          </CardContent>
        </Card>
      </form>

      {checks ? <Card><CardHeader><CardTitle>{zh ? "最近验证结果" : "Latest verification"}</CardTitle><CardDescription>{checks.checkedAt ? new Date(checks.checkedAt * 1000).toLocaleString(zh ? "zh-CN" : "en-US") : "-"}</CardDescription></CardHeader><CardContent className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{Object.entries(checks.checks || {}).map(([id, item]) => <div key={id} className="rounded-lg border bg-muted/20 p-3"><div className="flex items-center justify-between gap-2"><strong className="capitalize">{id}</strong><Badge variant={checkVariant(item.status)}>{item.status || "unknown"}</Badge></div><p className="mt-2 text-sm text-muted-foreground">{item.detail || "-"}</p></div>)}</CardContent></Card> : null}
    </div>
  );
}
