import { useEffect, useMemo, useState, type FormEvent } from "react";
import { AlertCircle, CheckCircle2, Cloud, KeyRound, Network, RefreshCw, ShieldCheck } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
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

function checkLabel(status: string | undefined, zh: boolean) {
  const labels: Record<string, [string, string]> = {
    ready: ["已就绪", "Ready"],
    missing: ["缺少配置", "Missing"],
    error: ["验证失败", "Failed"],
    skipped: ["已跳过", "Skipped"],
  };
  return labels[status || ""]?.[zh ? 0 : 1] || status || (zh ? "未知" : "Unknown");
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
  const [registryInvalid, setRegistryInvalid] = useState(false);

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

  const update = (key: keyof SetupForm, value: string) => {
    setForm((current) => ({ ...current, [key]: value }));
    if (key === "registryHost" || key === "pushHost") setRegistryInvalid(false);
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setNotice("");
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
      setRegistryInvalid(true);
      setError(zh ? "registryHost 和 pushHost 需要同时填写。" : "registryHost and pushHost must be provided together.");
      return;
    }
    setRegistryInvalid(false);
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
    setNotice("");
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
        metrics: [{ label: zh ? "已就绪" : "Ready", value: readiness ? `${configuredCount}/${integrationDefinitions.length}` : "-" }],
      }} />

      {error ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "保存或验证失败" : "Save or verification failed"}</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}
      {notice ? <Alert role="status"><CheckCircle2 /><AlertDescription>{notice}</AlertDescription></Alert> : null}

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5" aria-label={zh ? "依赖状态" : "Dependency status"}>
        {integrationDefinitions.map(({ id, zh: labelZh, en: labelEn, icon: Icon }) => {
          const status = statusFor(setupReadiness[id]);
          return <Card key={id} size="sm"><CardHeader><CardTitle>{zh ? labelZh : labelEn}</CardTitle><CardDescription>{readiness ? (setupReadiness[id]?.configured ? (zh ? "可供控制面使用" : "Available to Control") : (zh ? "尚未配置" : "Not configured")) : (zh ? "正在读取状态" : "Loading status")}</CardDescription></CardHeader><CardContent>{readiness ? <Badge variant={status.variant}><Icon />{zh ? status.zh : status.en}</Badge> : <Skeleton className="h-5 w-20" />}</CardContent></Card>;
        })}
      </section>

      <form onSubmit={submit}>
        <FieldGroup className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Cloudflare</CardTitle><CardDescription>{zh ? "用于 DNS、边缘入口和证书自动化。Token 只写入，不会回显。" : "Used for DNS, edge routes, and certificate automation. The token is write-only."}</CardDescription></CardHeader>
          <CardContent><FieldSet><FieldLegend>{zh ? "边缘与 DNS" : "Edge and DNS"}</FieldLegend><FieldDescription>{zh ? "Zone 与边缘目标会写入控制面配置。" : "The zone and edge target are saved to Control configuration."}</FieldDescription><FieldGroup>
            <Field data-disabled={busy !== ""}><FieldLabel htmlFor="cloudflare-token">API Token</FieldLabel><Input disabled={busy !== ""} id="cloudflare-token" type="password" autoComplete="new-password" placeholder="CLOUDFLARE_API_TOKEN" value={form.cloudflareToken} onChange={(event) => update("cloudflareToken", event.target.value)} /></Field>
            <Field data-disabled={busy !== ""}><FieldLabel htmlFor="cloudflare-zone">Zone</FieldLabel><Input disabled={busy !== ""} id="cloudflare-zone" placeholder={zh ? "例如 example.com" : "e.g. example.com"} value={form.cloudflareZone} onChange={(event) => update("cloudflareZone", event.target.value)} /></Field>
            <Field data-disabled={busy !== ""}><FieldLabel htmlFor="cloudflare-zone-id">Zone ID</FieldLabel><Input disabled={busy !== ""} id="cloudflare-zone-id" placeholder="CLOUDFLARE_ZONE_ID" value={form.cloudflareZoneId} onChange={(event) => update("cloudflareZoneId", event.target.value)} /></Field>
            <Field data-disabled={busy !== ""}><FieldLabel htmlFor="edge-target">Edge target</FieldLabel><Input disabled={busy !== ""} id="edge-target" placeholder={zh ? "例如 203.0.113.10" : "e.g. 203.0.113.10"} value={form.edgeTarget} onChange={(event) => update("edgeTarget", event.target.value)} /></Field>
          </FieldGroup></FieldSet></CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>{zh ? "节点与证书" : "Nodes and certificates"}</CardTitle><CardDescription>{zh ? "跨网络接入和 HTTPS 自动签发使用这些配置。" : "These values enable cross-network joins and automated HTTPS certificates."}</CardDescription></CardHeader>
          <CardContent><FieldGroup>
            <Field data-disabled={busy !== ""}><FieldLabel htmlFor="tailscale-authkey">Tailscale auth key</FieldLabel><Input disabled={busy !== ""} id="tailscale-authkey" type="password" autoComplete="new-password" placeholder="TAILSCALE_AUTHKEY" value={form.tailscaleAuthKey} onChange={(event) => update("tailscaleAuthKey", event.target.value)} /><FieldDescription>{zh ? "用于跨网络节点接入和内部服务互联。" : "Used for cross-network node joins and private service connectivity."}</FieldDescription></Field>
            <Field data-disabled={busy !== ""}><FieldLabel htmlFor="acme-email">ACME email</FieldLabel><Input disabled={busy !== ""} id="acme-email" type="email" placeholder="TRAEFIK_ACME_EMAIL" value={form.acmeEmail} onChange={(event) => update("acmeEmail", event.target.value)} /><FieldDescription>{zh ? "用于 Traefik 自动签发 HTTPS 证书。" : "Used by Traefik to issue HTTPS certificates."}</FieldDescription></Field>
          </FieldGroup></CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>{zh ? "可选出网" : "Optional egress"}</CardTitle><CardDescription>{zh ? "需要代理出网或大陆节点拉取镜像时填写。" : "Use this when workloads or mainland nodes need proxy egress."}</CardDescription></CardHeader>
          <CardContent><FieldGroup><Field data-disabled={busy !== ""}><FieldLabel htmlFor="egress-subscription">Subscription URL</FieldLabel><Input disabled={busy !== ""} id="egress-subscription" type="url" placeholder="EGRESS_SUBSCRIPTION_URL" value={form.egressSubscriptionUrl} onChange={(event) => update("egressSubscriptionUrl", event.target.value)} /></Field></FieldGroup></CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>{zh ? "Builder 镜像仓库" : "Builder registry"}</CardTitle><CardDescription>{zh ? "构建节点必须同时知道拉取地址和推送地址。" : "Builder nodes need both a pull address and a push address."}</CardDescription></CardHeader>
          <CardContent><FieldGroup>
            <Field data-disabled={busy !== ""} data-invalid={registryInvalid && !form.registryHost.trim()}><FieldLabel htmlFor="registry-host">{zh ? "拉取地址（registryHost）" : "Pull address (registryHost)"}</FieldLabel><Input disabled={busy !== ""} id="registry-host" placeholder="100.64.0.10:5000" value={form.registryHost} aria-invalid={registryInvalid && !form.registryHost.trim()} aria-describedby={registryInvalid && !form.registryHost.trim() ? "registry-host-error" : undefined} onChange={(event) => update("registryHost", event.target.value)} />{registryInvalid && !form.registryHost.trim() ? <FieldError id="registry-host-error">{zh ? "请填写镜像拉取地址。" : "Enter the image pull address."}</FieldError> : null}</Field>
            <Field data-disabled={busy !== ""} data-invalid={registryInvalid && !form.pushHost.trim()}><FieldLabel htmlFor="push-host">{zh ? "推送地址（pushHost）" : "Push address (pushHost)"}</FieldLabel><Input disabled={busy !== ""} id="push-host" placeholder="100.64.0.10:5000" value={form.pushHost} aria-invalid={registryInvalid && !form.pushHost.trim()} aria-describedby={registryInvalid && !form.pushHost.trim() ? "push-host-error" : undefined} onChange={(event) => update("pushHost", event.target.value)} />{registryInvalid && !form.pushHost.trim() ? <FieldError id="push-host-error">{zh ? "请填写镜像推送地址。" : "Enter the image push address."}</FieldError> : null}</Field>
          </FieldGroup></CardContent>
        </Card>

        <Card className="xl:col-span-2">
          <CardHeader><CardTitle>{zh ? "保存与验证" : "Save and verify"}</CardTitle><CardDescription>{zh ? "空白字段不会覆盖已有 Secret；敏感值只写入不回显。验证使用已保存的配置。" : "Blank fields do not overwrite existing secrets. Sensitive values are write-only. Verification uses the saved configuration."}</CardDescription></CardHeader>
          <CardFooter className="flex-wrap gap-2">
            <Button type="submit" disabled={busy !== ""}>{busy === "save" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}{busy === "save" ? (zh ? "保存中…" : "Saving…") : (zh ? "保存配置" : "Save configuration")}</Button>
            <Button type="button" variant="outline" disabled={busy !== ""} onClick={() => void verify()}>{busy === "check" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <RefreshCw data-icon="inline-start" />}{busy === "check" ? (zh ? "验证中…" : "Checking…") : (zh ? "立即验证依赖" : "Verify dependencies")}</Button>
          </CardFooter>
        </Card>
        </FieldGroup>
      </form>

      <Card>
        <CardHeader><CardTitle>{zh ? "最近验证结果" : "Latest verification"}</CardTitle><CardDescription>{checks?.checkedAt ? new Date(checks.checkedAt * 1000).toLocaleString(zh ? "zh-CN" : "en-US") : (zh ? "保存配置后，运行依赖验证以确认是否可用。" : "Save the configuration, then verify that the dependencies are available.")}</CardDescription></CardHeader>
        <CardContent>
          {Object.keys(checks?.checks || {}).length ? <Table aria-label={zh ? "依赖验证结果" : "Dependency verification results"} containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "依赖验证列表" : "Dependency verification list" }}>
            <TableHeader><TableRow><TableHead>{zh ? "依赖" : "Dependency"}</TableHead><TableHead>{zh ? "状态" : "Status"}</TableHead><TableHead>{zh ? "说明" : "Details"}</TableHead></TableRow></TableHeader>
            <TableBody>{Object.entries(checks?.checks || {}).map(([id, item]) => <TableRow key={id}><TableCell>{integrationDefinitions.find((definition) => definition.id === id)?.[zh ? "zh" : "en"] || id}</TableCell><TableCell><Badge variant={checkVariant(item.status)}>{checkLabel(item.status, zh)}</Badge></TableCell><TableCell className="min-w-48 whitespace-normal break-words">{item.detail || "-"}</TableCell></TableRow>)}</TableBody>
          </Table> : busy === "check" ? <div className="flex flex-col gap-3" role="status" aria-label={zh ? "正在验证依赖" : "Verifying dependencies"}><Skeleton className="h-8 w-full" /><Skeleton className="h-8 w-full" /><Skeleton className="h-8 w-3/4" /></div> : <Empty><EmptyHeader><EmptyMedia variant="icon"><ShieldCheck /></EmptyMedia><EmptyTitle>{zh ? "暂无验证结果" : "No verification results"}</EmptyTitle><EmptyDescription>{zh ? "选择“立即验证依赖”后，结果会显示在这里。" : "Choose Verify dependencies to see the results here."}</EmptyDescription></EmptyHeader></Empty>}
        </CardContent>
      </Card>
    </div>
  );
}
