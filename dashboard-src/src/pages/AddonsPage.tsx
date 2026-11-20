import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  Activity,
  ArrowUpRight,
  Boxes,
  Check,
  Copy,
  KeyRound,
  Package,
  RefreshCw,
  Settings,
  ShieldCheck,
  Trash2,
  TriangleAlert,
} from "@/components/icons";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { apiGet, apiPost, authHeaders, consumeNdjson } from "../apiClient";
import type { Lang } from "../types";
import { PageHeader } from "./PageHeader";

type LocalizedText = { en?: string; zh?: string };

type AddonPreset = {
  id: string;
  label?: LocalizedText;
  description?: LocalizedText;
  defaultRegion?: string;
  defaultExposure?: string;
  defaultNode?: string;
};

type AddonConfigField = {
  key: string;
  type: "string" | "domain" | "region" | "node" | "select" | "integer" | "boolean" | "secret";
  group?: string;
  required?: boolean;
  default?: string | number | boolean;
  env?: string;
  placeholder?: string;
  options?: string[];
  min?: number;
  max?: number;
  label?: LocalizedText;
  description?: LocalizedText;
};

type AddonAutoSecret = {
  name: string;
  description?: string;
};

type AddonItem = {
  id: string;
  name: string;
  category: string;
  description: LocalizedText;
  stack: string;
  sourceDir: string;
  installed: boolean;
  configured: boolean;
  autoDiscovered: boolean;
  status: "running" | "configured" | "not_installed" | string;
  preset: string;
  url: string;
  consoleUrl: string;
  auth: {
    mode: "control-sso" | "forward-auth" | "none" | string;
    cliAuth?: string;
  };
  ssoReady: boolean;
  ui: {
    icon?: string;
    openMode?: "external" | "internal";
    dashboardPath?: string;
  };
  presets: AddonPreset[];
  configSchema: AddonConfigField[];
  autoSecrets: AddonAutoSecret[];
  config: Record<string, string | number | boolean>;
  secretsStatus: Record<string, boolean>;
  canApplyInPlace: boolean;
};

type AddonsPayload = {
  addons: AddonItem[];
  controlUrl: string;
  availableRegions: string[];
  availableNodes: string[];
};

function loc(text: LocalizedText | undefined, lang: Lang, fallback = ""): string {
  if (!text) return fallback;
  return (lang === "zh" ? text.zh || text.en : text.en || text.zh) || fallback;
}

function AddonIcon({ icon }: { icon?: string }) {
  if (icon === "builder") return <Boxes className="size-4 text-primary" />;
  if (icon === "observe") return <Activity className="size-4 text-primary" />;
  return <Package className="size-4 text-primary" />;
}

export function AddonsPage({
  lang,
  token,
  onUnauthorized,
}: {
  lang: Lang;
  token: string;
  onUnauthorized?: () => void;
}) {
  const zh = lang === "zh";
  const [data, setData] = useState<AddonsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [copiedId, setCopiedId] = useState("");

  // Configuration dialog state
  const [editing, setEditing] = useState<AddonItem | null>(null);
  const [preset, setPreset] = useState("");
  const [formConfig, setFormConfig] = useState<Record<string, string>>({});
  const [formSecrets, setFormSecrets] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<"" | "save" | "apply">("");
  const [saveError, setSaveError] = useState("");
  const [deployLogs, setDeployLogs] = useState<string[]>([]);

  // Uninstall confirmation dialog state
  const [removing, setRemoving] = useState<AddonItem | null>(null);
  const [purgeSecrets, setPurgeSecrets] = useState(false);
  const [uninstalling, setUninstalling] = useState(false);

  const loadAddons = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const payload = await apiGet<AddonsPayload>("/v1/addons", token);
      setData(payload);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.toLowerCase().includes("unauthorized") || msg.includes("401")) {
        onUnauthorized?.();
      }
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [token, onUnauthorized]);

  useEffect(() => {
    void loadAddons();
  }, [loadAddons]);

  const openEditor = (addon: AddonItem) => {
    setEditing(addon);
    setPreset(addon.preset || addon.presets[0]?.id || "default");
    const nextCfg: Record<string, string> = {};
    for (const field of addon.configSchema) {
      if (field.type === "secret") continue;
      const val = addon.config?.[field.key];
      nextCfg[field.key] = val !== undefined && val !== null ? String(val) : String(field.default ?? "");
    }
    setFormConfig(nextCfg);
    setFormSecrets({});
    setSaveError("");
    setDeployLogs([]);
  };

  const handlePresetChange = (nextPreset: string) => {
    setPreset(nextPreset);
    if (!editing) return;
    const found = editing.presets.find((item) => item.id === nextPreset);
    if (!found) return;
    setFormConfig((prev) => ({
      ...prev,
      ...(found.defaultRegion ? { region: found.defaultRegion } : {}),
      ...(found.defaultExposure ? { exposure: found.defaultExposure } : {}),
      ...(found.defaultNode !== undefined && !prev.node ? { node: found.defaultNode } : {}),
    }));
  };

  const buildPayload = () => {
    if (!editing) return {};
    const cfg: Record<string, string | number | boolean> = {};
    for (const field of editing.configSchema) {
      if (field.type === "secret") continue;
      const raw = formConfig[field.key] ?? "";
      if (field.type === "integer") {
        if (raw.trim() !== "") cfg[field.key] = Number.parseInt(raw.trim(), 10);
      } else if (field.type === "boolean") {
        cfg[field.key] = raw === "true";
      } else {
        cfg[field.key] = raw.trim();
      }
    }
    const secrets: Record<string, string> = {};
    for (const [envName, val] of Object.entries(formSecrets)) {
      if (val.trim()) secrets[envName] = val.trim();
    }
    return { preset, config: cfg, secrets, origin: "dashboard" };
  };

  const handleSave = async (applyNow: boolean) => {
    if (!editing) return;
    setSaving(applyNow ? "apply" : "save");
    setSaveError("");
    setDeployLogs([]);
    const payload = buildPayload();
    try {
      if (applyNow) {
        const response = await fetch(`/v1/addons/${encodeURIComponent(editing.id)}/apply/stream`, {
          method: "POST",
          headers: authHeaders(token, true),
          body: JSON.stringify(payload),
        });
        await consumeNdjson(response, (ev) => {
          const name = String(ev.name || ev.stage || "");
          const msg = String(ev.message || "");
          if (name || msg) {
            setDeployLogs((prev) => [...prev, `[${String(ev.status || "step")}] ${name}${msg ? `: ${msg}` : ""}`]);
          }
        });
        setNotice(zh ? `${editing.name} 配置已保存并完成部署。` : `${editing.name} configured and deployed.`);
      } else {
        const res = await apiPost<{ generatedSecrets?: string[] }>(
          `/v1/addons/${encodeURIComponent(editing.id)}/configure`,
          token,
          payload,
        );
        const generated = res.generatedSecrets?.length
          ? (zh ? `（自动生成内部密钥：${res.generatedSecrets.join(", ")}）` : ` (auto-generated secrets: ${res.generatedSecrets.join(", ")})`)
          : "";
        setNotice(zh ? `${editing.name} 配置已保存${generated}。` : `${editing.name} configuration saved${generated}.`);
      }
      await loadAddons();
      if (!applyNow) setEditing(null);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving("");
    }
  };

  const handleUninstall = async () => {
    if (!removing) return;
    setUninstalling(true);
    setError("");
    try {
      await apiPost(`/v1/addons/${encodeURIComponent(removing.id)}/uninstall`, token, {
        purgeSecrets,
        deleteStorage: false,
      });
      setNotice(
        zh
          ? `已卸载 ${removing.name} 并吊销其单点登录会话（数据卷已保留）。`
          : `Uninstalled ${removing.name} and revoked SSO sessions (data volumes preserved).`,
      );
      setRemoving(null);
      setPurgeSecrets(false);
      await loadAddons();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setUninstalling(false);
    }
  };

  const copyCommand = async (id: string, cmd: string) => {
    try {
      await navigator.clipboard.writeText(cmd);
      setCopiedId(id);
      window.setTimeout(() => setCopiedId(""), 1800);
    } catch {
      // clipboard unavailable
    }
  };

  const addons = data?.addons || [];
  const installedCount = addons.filter((a) => a.installed).length;
  const ssoCount = addons.filter((a) => a.ssoReady && a.installed).length;

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader
        meta={{
          eyebrow: zh ? "平台管理 / 插件中心" : "Platform / Addons",
          title: zh ? "插件中心" : "Addons",
          description: zh
            ? "可插拔管理 Luma 官方扩展插件，统一接入 Luma Control 单点登录（Passkey 与 CLI Token），自动托管内部密钥。"
            : "Install, configure, and sign in to pluggable Luma extensions with unified Control SSO and auto-managed internal secrets.",
          metrics: [
            { label: zh ? "已运行" : "Running", value: `${installedCount} / ${addons.length}` },
            { label: zh ? "统一认证就绪" : "Unified SSO", value: ssoCount },
          ],
          action: (
            <Button variant="outline" size="sm" disabled={loading} onClick={() => void loadAddons()}>
              <RefreshCw className="size-3.5" />
              {zh ? "刷新" : "Refresh"}
            </Button>
          ),
        }}
      />

      {notice ? (
        <Alert>
          <ShieldCheck className="size-4" />
          <AlertTitle>{zh ? "已完成" : "Success"}</AlertTitle>
          <AlertDescription>{notice}</AlertDescription>
        </Alert>
      ) : null}

      {error ? (
        <Alert variant="destructive">
          <TriangleAlert className="size-4" />
          <AlertTitle>{zh ? "无法加载插件状态" : "Failed to load addons"}</AlertTitle>
          <AlertDescription className="wrap-anywhere">{error}</AlertDescription>
        </Alert>
      ) : null}

      {loading && addons.length === 0 ? (
        <div className="grid gap-4 md:grid-cols-3">
          <Skeleton className="h-56 w-full rounded-xl" />
          <Skeleton className="h-56 w-full rounded-xl" />
          <Skeleton className="h-56 w-full rounded-xl" />
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {addons.map((addon) => {
            const installCmd = `luma addon install ${addon.id}${addon.sourceDir ? ` --source ./${addon.sourceDir}` : ""}`;
            const authLabel =
              addon.auth.mode === "control-sso"
                ? "Control SSO"
                : addon.auth.mode === "forward-auth"
                  ? "ForwardAuth"
                  : zh
                    ? "独立认证"
                    : "Standalone";
            const statusVariant = addon.installed ? "default" : addon.configured ? "secondary" : "outline";
            const statusText = addon.installed
              ? zh
                ? "运行中"
                : "Running"
              : addon.configured
                ? zh
                  ? "已配置"
                  : "Configured"
                : zh
                  ? "未安装"
                  : "Not installed";

            return (
              <Card key={addon.id} className="flex flex-col justify-between">
                <CardHeader className="gap-2">
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex items-center gap-2.5">
                      <span className="flex size-8 items-center justify-center rounded-lg bg-muted ring-1 ring-border">
                        <AddonIcon icon={addon.ui.icon} />
                      </span>
                      <div>
                        <CardTitle className="text-base">{addon.name}</CardTitle>
                        <div className="font-mono text-xs text-muted-foreground">{addon.stack}</div>
                      </div>
                    </div>
                    <Badge variant={statusVariant}>{statusText}</Badge>
                  </div>
                  <CardDescription className="text-xs/5">{loc(addon.description, lang)}</CardDescription>
                </CardHeader>

                <CardContent className="flex flex-col gap-4">
                  <div className="flex flex-col gap-1.5 rounded-lg border border-border bg-muted/35 p-3 text-xs">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-muted-foreground">{zh ? "鉴权模式" : "Auth"}</span>
                      <span className="flex items-center gap-1 font-medium text-foreground">
                        <ShieldCheck className="size-3.5 text-emerald-600 dark:text-emerald-400" />
                        {authLabel}
                      </span>
                    </div>
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-muted-foreground">{zh ? "部署预设" : "Preset"}</span>
                      <span className="font-mono text-foreground">{addon.preset}</span>
                    </div>
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-muted-foreground">{zh ? "访问入口" : "Endpoint"}</span>
                      <span className="max-w-[190px] truncate font-mono text-foreground" title={addon.consoleUrl || addon.url || "-"}>
                        {addon.url || (zh ? "未配置" : "Not set")}
                      </span>
                    </div>
                  </div>

                  {!addon.installed ? (
                    <div className="flex items-center justify-between gap-2 rounded-md border border-dashed border-border bg-background px-2.5 py-1.5 font-mono text-[0.7rem] text-muted-foreground">
                      <span className="truncate">{installCmd}</span>
                      <button
                        type="button"
                        onClick={() => void copyCommand(addon.id, installCmd)}
                        className="shrink-0 text-muted-foreground hover:text-foreground"
                        aria-label={zh ? "复制安装命令" : "Copy install command"}
                      >
                        {copiedId === addon.id ? <Check className="size-3.5 text-emerald-600" /> : <Copy className="size-3.5" />}
                      </button>
                    </div>
                  ) : null}

                  <div className="flex flex-wrap items-center gap-2">
                    {addon.installed && addon.consoleUrl ? (
                      <Button
                        size="sm"
                        nativeButton={false}
                        render={
                          <a
                            href={addon.consoleUrl}
                            target={addon.ui.openMode === "external" ? "_blank" : undefined}
                            rel={addon.ui.openMode === "external" ? "noreferrer" : undefined}
                          />
                        }
                      >
                        {zh ? "打开控制台" : "Open console"}
                        <ArrowUpRight className="ml-1 size-3.5" />
                      </Button>
                    ) : null}
                    <Button size="sm" variant={addon.installed ? "outline" : "default"} onClick={() => openEditor(addon)}>
                      <Settings className="mr-1 size-3.5" />
                      {addon.installed ? (zh ? "配置" : "Configure") : zh ? "配置安装" : "Configure"}
                    </Button>
                    {addon.installed ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="ml-auto text-destructive hover:text-destructive"
                        onClick={() => {
                          setRemoving(addon);
                          setPurgeSecrets(false);
                        }}
                      >
                        <Trash2 className="size-3.5" />
                      </Button>
                    ) : null}
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {/* Schema-driven Configuration Dialog */}
      <Dialog open={Boolean(editing)} onOpenChange={(open) => { if (!open && !saving) setEditing(null); }}>
        <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
          {editing ? (
            <form
              onSubmit={(ev: FormEvent) => {
                ev.preventDefault();
                void handleSave(false);
              }}
            >
              <DialogHeader>
                <DialogTitle>
                  {zh ? `配置 ${editing.name}` : `Configure ${editing.name}`}
                </DialogTitle>
                <DialogDescription>
                  {zh
                    ? "无需手改 YAML 或手动生成内部密钥。保存时将自动注入 LUMA_CONTROL_URL 并初始化缺失的内部密钥。"
                    : "No manual YAML edits required. Saving auto-wires LUMA_CONTROL_URL and generates any missing internal secrets."}
                </DialogDescription>
              </DialogHeader>

              <div className="my-4 flex flex-col gap-4">
                {saveError ? (
                  <Alert variant="destructive">
                    <TriangleAlert className="size-4" />
                    <AlertTitle>{zh ? "保存失败" : "Failed"}</AlertTitle>
                    <AlertDescription className="wrap-anywhere">{saveError}</AlertDescription>
                  </Alert>
                ) : null}

                <FieldGroup className="gap-3.5">
                  {editing.presets.length > 1 ? (
                    <Field>
                      <FieldLabel htmlFor="addon-preset">{zh ? "部署拓扑预设" : "Deployment preset"}</FieldLabel>
                      <select
                        id="addon-preset"
                        value={preset}
                        onChange={(ev) => handlePresetChange(ev.target.value)}
                        className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        {editing.presets.map((p) => (
                          <option key={p.id} value={p.id}>
                            {loc(p.label, lang, p.id)} ({p.id})
                          </option>
                        ))}
                      </select>
                      <FieldDescription>
                        {loc(editing.presets.find((p) => p.id === preset)?.description, lang)}
                      </FieldDescription>
                    </Field>
                  ) : null}

                  {editing.configSchema.map((field) => {
                    const label = loc(field.label, lang, field.key);
                    const desc = loc(field.description, lang);
                    if (field.type === "secret") {
                      const envName = field.env || "";
                      const isSet = Boolean(editing.secretsStatus?.[envName]);
                      return (
                        <Field key={field.key}>
                          <div className="flex items-center justify-between gap-2">
                            <FieldLabel htmlFor={`field-${field.key}`}>{label}</FieldLabel>
                            <Badge variant={isSet ? "secondary" : "outline"} className="text-[0.6875rem]">
                              {isSet ? (zh ? "已配置" : "Configured") : zh ? "未设置" : "Unset"}
                            </Badge>
                          </div>
                          <Input
                            id={`field-${field.key}`}
                            type="password"
                            value={formSecrets[envName] || ""}
                            onChange={(ev) => setFormSecrets((prev) => ({ ...prev, [envName]: ev.target.value }))}
                            placeholder={isSet ? (zh ? "留空保持现有密钥不变" : "Leave blank to keep existing secret") : field.placeholder || ""}
                            autoComplete="new-password"
                          />
                          {desc ? <FieldDescription>{desc}</FieldDescription> : null}
                        </Field>
                      );
                    }

                    if (field.type === "select" || field.type === "region" || field.type === "node") {
                      const options =
                        field.type === "select"
                          ? field.options || []
                          : field.type === "region"
                            ? data?.availableRegions || ["cn", "global", "home"]
                            : ["", ...(data?.availableNodes || [])];
                      return (
                        <Field key={field.key}>
                          <FieldLabel htmlFor={`field-${field.key}`}>{label}</FieldLabel>
                          <select
                            id={`field-${field.key}`}
                            value={formConfig[field.key] ?? ""}
                            onChange={(ev) => setFormConfig((prev) => ({ ...prev, [field.key]: ev.target.value }))}
                            className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          >
                            {options.map((opt) => (
                              <option key={opt || "__auto__"} value={opt}>
                                {opt || (zh ? "自动调度（不固定节点）" : "Auto-schedule (unpinned)")}
                              </option>
                            ))}
                          </select>
                          {desc ? <FieldDescription>{desc}</FieldDescription> : null}
                        </Field>
                      );
                    }

                    return (
                      <Field key={field.key}>
                        <FieldLabel htmlFor={`field-${field.key}`}>{label}</FieldLabel>
                        <Input
                          id={`field-${field.key}`}
                          type={field.type === "integer" ? "number" : "text"}
                          min={field.min}
                          max={field.max}
                          required={Boolean(field.required)}
                          placeholder={field.placeholder || ""}
                          value={formConfig[field.key] ?? ""}
                          onChange={(ev) => setFormConfig((prev) => ({ ...prev, [field.key]: ev.target.value }))}
                        />
                        {desc ? <FieldDescription>{desc}</FieldDescription> : null}
                      </Field>
                    );
                  })}
                </FieldGroup>

                {editing.autoSecrets.length > 0 ? (
                  <div className="rounded-lg border border-border bg-muted/35 p-3 text-xs">
                    <div className="mb-2 flex items-center gap-1.5 font-medium text-foreground">
                      <KeyRound className="size-3.5 text-muted-foreground" />
                      {zh ? "自动托管内部密钥" : "Auto-managed internal secrets"}
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {editing.autoSecrets.map((s) => (
                        <Badge key={s.name} variant={editing.secretsStatus?.[s.name] ? "secondary" : "outline"} className="font-mono text-[0.6875rem]">
                          {s.name}: {editing.secretsStatus?.[s.name] ? (zh ? "就绪" : "ready") : zh ? "保存时自动生成" : "auto-gen"}
                        </Badge>
                      ))}
                    </div>
                  </div>
                ) : null}

                {deployLogs.length > 0 ? (
                  <pre className="max-h-40 overflow-auto rounded-lg border border-border bg-muted/50 p-3 font-mono text-xs text-foreground">
                    {deployLogs.join("\n")}
                  </pre>
                ) : null}
              </div>

              <DialogFooter className="gap-2">
                <Button type="button" variant="outline" disabled={Boolean(saving)} onClick={() => setEditing(null)}>
                  {zh ? "关闭" : "Close"}
                </Button>
                <Button type="submit" variant={editing.canApplyInPlace ? "outline" : "default"} disabled={Boolean(saving)}>
                  {saving === "save" ? <Spinner className="mr-1.5 size-3.5" /> : null}
                  {zh ? "仅保存配置" : "Save config"}
                </Button>
                {editing.canApplyInPlace ? (
                  <Button type="button" disabled={Boolean(saving)} onClick={() => void handleSave(true)}>
                    {saving === "apply" ? <Spinner className="mr-1.5 size-3.5" /> : null}
                    {zh ? "保存并热更新部署" : "Save & redeploy"}
                  </Button>
                ) : null}
              </DialogFooter>
            </form>
          ) : null}
        </DialogContent>
      </Dialog>

      {/* Uninstall confirmation */}
      <AlertDialog open={Boolean(removing)} onOpenChange={(open) => { if (!open && !uninstalling) setRemoving(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {zh ? `卸载 ${removing?.name}？` : `Uninstall ${removing?.name}?`}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {zh
                ? "将停止并移除该插件服务及路由，并立即吊销其 Luma Control 单点登录会话。默认保留持久化数据卷。"
                : "Stops the addon services and routes and immediately revokes its Luma Control SSO sessions. Persistent data volumes are preserved."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={purgeSecrets}
              onChange={(ev) => setPurgeSecrets(ev.target.checked)}
            />
            {zh ? "同时清除该插件作用域下的 Secrets" : "Also delete scoped secrets for this addon"}
          </label>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={uninstalling}>{zh ? "取消" : "Cancel"}</AlertDialogCancel>
            <AlertDialogAction variant="destructive" disabled={uninstalling} onClick={() => void handleUninstall()}>
              {uninstalling ? <Spinner className="mr-1.5 size-3.5" /> : null}
              {zh ? "确认卸载" : "Uninstall"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
