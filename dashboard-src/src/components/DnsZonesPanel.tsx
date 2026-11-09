import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, Plus, Trash2 } from "@/components/icons";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldDescription, FieldGroup, FieldLabel, FieldTitle } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useConfirm } from "./ConfirmDialog";
import { SelectControl } from "./primitives";
import { addDnsZone, checkDnsZone, fetchDnsZones, removeDnsZone, type DnsProviderId, type DnsZone } from "../dnsApi";
import { formatDateTime } from "../format";
import type { Lang } from "../types";

const PROVIDERS: DnsProviderId[] = ["cloudflare", "alidns", "dnspod"];

const PROVIDER_COPY: Record<DnsProviderId, { label: [string, string]; fields: Array<{ id: string; label: string }>; hint: [string, string] }> = {
  cloudflare: {
    label: ["Cloudflare", "Cloudflare"],
    fields: [{ id: "apiToken", label: "API Token" }],
    hint: [
      "Create a token with Zone → DNS → Edit for this zone in My Profile → API Tokens.",
      "在 My Profile → API Tokens 创建令牌，权限选 Zone → DNS → Edit，并限定到这个域名。",
    ],
  },
  alidns: {
    label: ["Alibaba Cloud DNS", "阿里云 DNS"],
    fields: [{ id: "accessKeyId", label: "AccessKey ID" }, { id: "accessKeySecret", label: "AccessKey Secret" }],
    hint: [
      "Use a RAM user with AliyunDNSFullAccess (or a policy limited to this domain), not the root account key.",
      "建议用 RAM 子账号，授予 AliyunDNSFullAccess（或仅限该域名的自定义策略），不要用主账号 AccessKey。",
    ],
  },
  dnspod: {
    label: ["Tencent Cloud DNSPod", "腾讯云 DNSPod"],
    fields: [{ id: "secretId", label: "SecretId" }, { id: "secretKey", label: "SecretKey" }],
    hint: [
      "Create a CAM sub-user key with QcloudDNSPodFullAccess in Access Management → API Keys.",
      "在访问管理 → API 密钥创建子用户密钥，授予 QcloudDNSPodFullAccess。",
    ],
  },
};

type FormState = {
  name: string;
  provider: DnsProviderId;
  mode: "new" | "shared";
  sameAs: string;
  values: Record<string, string>;
  update: boolean;
};

const EMPTY_FORM: FormState = { name: "", provider: "alidns", mode: "new", sameAs: "", values: {}, update: false };

function statusBadge(zone: DnsZone, zh: boolean) {
  if (zone.status === "ready") return <Badge>{zh ? "可用" : "Ready"}</Badge>;
  if (zone.status === "error") return <Badge variant="destructive">{zh ? "检查失败" : "Check failed"}</Badge>;
  if (zone.status === "missing-credentials" || zone.credentialsConfigured === false) return <Badge variant="destructive">{zh ? "缺少凭据" : "Missing key"}</Badge>;
  return <Badge variant="outline">{zone.status || "-"}</Badge>;
}

export function DnsZonesPanel({ lang, token }: { lang: Lang; token: string }) {
  const zh = lang === "zh";
  const { confirm, element } = useConfirm(lang);
  const [zones, setZones] = useState<DnsZone[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [form, setForm] = useState<FormState>(EMPTY_FORM);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const payload = await fetchDnsZones(token, signal);
      setZones(payload.zones || []);
      setError("");
    } catch (exc) {
      if (signal?.aborted) return;
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const copy = PROVIDER_COPY[form.provider];
  const shareable = useMemo(() => zones.filter((zone) => zone.provider === form.provider && !zone.primary && zone.name !== form.name.trim().toLowerCase()), [zones, form.provider, form.name]);
  const valuesComplete = copy.fields.every((field) => (form.values[field.id] || "").trim());
  // Updating a zone keeps any key field left empty, so a partial rotation is fine.
  const canSubmit = Boolean(form.name.trim()) && (form.mode === "shared" ? Boolean(form.sameAs) : form.update ? copy.fields.some((field) => (form.values[field.id] || "").trim()) : valuesComplete);

  const patch = (next: Partial<FormState>) => setForm((current) => ({ ...current, ...next }));

  const submit = async () => {
    setBusy("add");
    setError("");
    setNotice("");
    try {
      const credentials = Object.fromEntries(Object.entries(form.values).map(([key, value]) => [key, value.trim()]).filter(([, value]) => value));
      const result = await addDnsZone({
        token,
        name: form.name.trim(),
        provider: form.provider,
        credentials: form.mode === "new" ? credentials : undefined,
        sameAs: form.mode === "shared" ? form.sameAs : undefined,
        update: form.update,
      });
      setNotice(zh
        ? `${result.zone?.name || form.name} 已${form.update ? "更新" : "添加"}。之后部署到这个域名下的服务会自动在 ${copy.label[1]} 写解析记录。`
        : `${result.zone?.name || form.name} ${form.update ? "updated" : "added"}. Services under this domain publish their records on ${copy.label[0]} from the next deploy.`);
      setForm(EMPTY_FORM);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy("");
    }
  };

  const check = async (zone: DnsZone) => {
    setBusy(`check:${zone.name}`);
    setError("");
    setNotice("");
    try {
      const result = await checkDnsZone({ token, name: zone.name });
      if (result.status === "ready") setNotice(result.detail || zone.name);
      else setError(result.detail || (zh ? "检查失败" : "Check failed"));
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy("");
    }
  };

  const remove = async (zone: DnsZone) => {
    const ok = await confirm({
      title: zh ? `移除域名 ${zone.name}？` : `Remove zone ${zone.name}?`,
      body: zh
        ? <p>只从 Luma 移除这个域名和它单独使用的凭据，服务商那边已有的解析记录保持不变。</p>
        : <p>This removes the zone and any key only it uses from Luma. Records already at the provider stay as they are.</p>,
      confirmLabel: zh ? "移除" : "Remove",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(`remove:${zone.name}`);
    setError("");
    setNotice("");
    try {
      await removeDnsZone({ token, name: zone.name });
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy("");
    }
  };

  const rotate = (zone: DnsZone) => {
    setForm({ ...EMPTY_FORM, name: zone.name, provider: zone.provider, update: true });
    setNotice("");
    document.getElementById("dns-zone-name")?.focus();
  };

  return (
    <div className="flex flex-col gap-6">
      {element}
      <Card>
        <CardHeader>
          <CardTitle>{zh ? "域名" : "Domains"}</CardTitle>
          <CardDescription>
            {zh
              ? "部署时 Luma 按域名后缀找到对应的 DNS 服务商，自动写解析记录。主域名在首次安装中配置；其他域名可以托管在 Cloudflare、阿里云或腾讯云。"
              : "On deploy, Luma matches each domain to the zone that owns it and writes its records there. The primary zone comes from first install; other zones can live on Cloudflare, Alibaba Cloud or Tencent Cloud."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? <div className="flex items-center gap-2 text-sm text-muted-foreground"><Spinner aria-hidden="true" />{zh ? "加载中…" : "Loading…"}</div> : zones.length ? (
            <Table aria-label={zh ? "DNS 域名" : "DNS zones"} containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "域名列表" : "Zone list" }}>
              <TableHeader>
                <TableRow>
                  <TableHead>{zh ? "域名" : "Zone"}</TableHead>
                  <TableHead>{zh ? "服务商" : "Provider"}</TableHead>
                  <TableHead>{zh ? "状态" : "Status"}</TableHead>
                  <TableHead>{zh ? "使用中的应用" : "Used by"}</TableHead>
                  <TableHead>{zh ? "上次检查" : "Checked"}</TableHead>
                  <TableHead><span className="sr-only">{zh ? "操作" : "Actions"}</span></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {zones.map((zone) => {
                  const users = zone.services || [];
                  return (
                    <TableRow key={zone.name}>
                      <TableCell>
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium">{zone.name}</span>
                          {zone.primary ? <Badge variant="secondary">{zh ? "主域名" : "Primary"}</Badge> : null}
                        </div>
                      </TableCell>
                      <TableCell>{PROVIDER_COPY[zone.provider]?.label[zh ? 1 : 0] || zone.providerLabel || zone.provider}</TableCell>
                      <TableCell>
                        <div className="flex flex-col gap-1">
                          {statusBadge(zone, zh)}
                          {zone.status === "error" && zone.detail ? <span className="block max-w-56 truncate text-xs text-muted-foreground" title={zone.detail}>{zone.detail}</span> : null}
                        </div>
                      </TableCell>
                      <TableCell>{users.length ? <span title={users.join(", ")}>{users.length <= 2 ? users.join(", ") : `${users.slice(0, 2).join(", ")} +${users.length - 2}`}</span> : <span className="text-muted-foreground">—</span>}</TableCell>
                      <TableCell>{zone.checkedAt ? formatDateTime(zone.checkedAt, lang) : <span className="text-muted-foreground">—</span>}</TableCell>
                      <TableCell>
                        <div className="flex justify-end gap-2 whitespace-nowrap">
                          <Button variant="outline" size="sm" disabled={Boolean(busy)} onClick={() => void check(zone)}>
                            {busy === `check:${zone.name}` ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}
                            {zh ? "检查" : "Check"}
                          </Button>
                          {zone.primary ? null : <>
                            <Button variant="outline" size="sm" disabled={Boolean(busy)} onClick={() => rotate(zone)}>{zh ? "更换凭据" : "Rotate key"}</Button>
                            <Button
                              variant="destructive"
                              size="sm"
                              disabled={Boolean(busy) || users.length > 0}
                              title={users.length ? (zh ? "仍有应用使用这个域名" : "Applications still use this zone") : undefined}
                              aria-label={zh ? `移除域名 ${zone.name}` : `Remove zone ${zone.name}`}
                              onClick={() => void remove(zone)}
                            >
                              {busy === `remove:${zone.name}` ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <Trash2 data-icon="inline-start" />}
                              {zh ? "移除" : "Remove"}
                            </Button>
                          </>}
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          ) : (
            <p className="text-sm text-muted-foreground">{zh ? "还没有域名。在首次安装中配置主域名，或在下面添加。" : "No zones yet. Configure the primary zone in first install, or add one below."}</p>
          )}
        </CardContent>
      </Card>

      {error ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "域名操作失败" : "Zone operation failed"}</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}
      {notice ? <Alert><AlertCircle /><AlertTitle>{zh ? "完成" : "Done"}</AlertTitle><AlertDescription>{notice}</AlertDescription></Alert> : null}

      <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
        <Card>
          <CardHeader>
            <CardTitle>{form.update ? (zh ? `更换 ${form.name} 的凭据` : `Rotate the key for ${form.name}`) : (zh ? "添加域名" : "Add a zone")}</CardTitle>
            <CardDescription>
              {zh
                ? "保存前会先用凭据调用一次服务商接口，确认能读写这个域名的解析。凭据存为 Control 密钥，之后不会再显示。"
                : "Luma calls the provider once with the key before saving to confirm it can manage this zone. Keys are stored as Control secrets and never shown again."}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <FieldGroup className="grid items-start gap-4 md:grid-cols-2">
              <Field data-disabled={Boolean(busy) || form.update}>
                <FieldLabel htmlFor="dns-zone-name">{zh ? "域名" : "Zone"}</FieldLabel>
                <Input id="dns-zone-name" value={form.name} onChange={(event) => patch({ name: event.target.value })} placeholder="example.cn" autoCapitalize="none" autoCorrect="off" spellCheck={false} disabled={Boolean(busy) || form.update} />
                <FieldDescription>{zh ? "填注册的主域名，例如 example.cn；其下所有子域名都会使用它。" : "The registered domain, e.g. example.cn. Every subdomain under it uses this zone."}</FieldDescription>
              </Field>
              <Field data-disabled={Boolean(busy) || form.update}>
                <FieldTitle id="dns-provider-label">{zh ? "DNS 服务商" : "DNS provider"}</FieldTitle>
                <ToggleGroup
                  aria-labelledby="dns-provider-label"
                  variant="segmented"
                  value={[form.provider]}
                  disabled={Boolean(busy) || form.update}
                  onValueChange={(values) => { if (values[0]) patch({ provider: values[0] as DnsProviderId, values: {}, sameAs: "", mode: "new" }); }}
                >
                  {PROVIDERS.map((provider) => <ToggleGroupItem key={provider} value={provider}>{PROVIDER_COPY[provider].label[zh ? 1 : 0]}</ToggleGroupItem>)}
                </ToggleGroup>
              </Field>
              {!form.update && shareable.length ? (
                <Field className="md:col-span-2" data-disabled={Boolean(busy)}>
                  <FieldTitle id="dns-credential-mode">{zh ? "凭据" : "Credentials"}</FieldTitle>
                  <ToggleGroup aria-labelledby="dns-credential-mode" variant="segmented" value={[form.mode]} disabled={Boolean(busy)} onValueChange={(values) => { if (values[0]) patch({ mode: values[0] as FormState["mode"] }); }}>
                    <ToggleGroupItem value="new">{zh ? "新的密钥" : "New key"}</ToggleGroupItem>
                    <ToggleGroupItem value="shared">{zh ? "与已有域名共用" : "Same account as another zone"}</ToggleGroupItem>
                  </ToggleGroup>
                </Field>
              ) : null}
              {form.mode === "shared" && !form.update ? (
                <Field data-disabled={Boolean(busy)}>
                  <FieldLabel htmlFor="dns-same-as">{zh ? "共用哪个域名的凭据" : "Reuse the key of"}</FieldLabel>
                  <SelectControl
                    id="dns-same-as"
                    value={form.sameAs}
                    options={[{ value: "", label: zh ? "选择域名" : "Choose a zone" }, ...shareable.map((zone) => ({ value: zone.name, label: zone.name }))]}
                    disabled={Boolean(busy)}
                    onChange={(value) => patch({ sameAs: value })}
                  />
                </Field>
              ) : copy.fields.map((field) => (
                <Field key={field.id} data-disabled={Boolean(busy)}>
                  <FieldLabel htmlFor={`dns-${field.id}`}>{field.label}</FieldLabel>
                  <Input
                    id={`dns-${field.id}`}
                    type="password"
                    autoComplete="new-password"
                    value={form.values[field.id] || ""}
                    onChange={(event) => patch({ values: { ...form.values, [field.id]: event.target.value } })}
                    placeholder={form.update ? (zh ? "留空保持不变" : "Leave empty to keep") : ""}
                    disabled={Boolean(busy)}
                  />
                </Field>
              ))}
            </FieldGroup>
            {form.mode === "new" || form.update ? <p className="mt-4 text-sm text-muted-foreground">{copy.hint[zh ? 1 : 0]}</p> : null}
          </CardContent>
          <CardFooter className="flex flex-wrap gap-2">
            <Button type="submit" disabled={Boolean(busy) || !canSubmit}>
              {busy === "add" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <Plus data-icon="inline-start" />}
              {busy === "add" ? (zh ? "验证中…" : "Verifying…") : form.update ? (zh ? "验证并更新" : "Verify and update") : (zh ? "验证并添加" : "Verify and add")}
            </Button>
            {form.update ? <Button type="button" variant="outline" disabled={Boolean(busy)} onClick={() => setForm(EMPTY_FORM)}>{zh ? "取消" : "Cancel"}</Button> : null}
          </CardFooter>
        </Card>
      </form>
    </div>
  );
}
