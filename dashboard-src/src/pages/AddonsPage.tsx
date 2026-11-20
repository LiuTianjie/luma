import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  Activity,
  ArrowUpRight,
  Boxes,
  Check,
  ChevronDown,
  Copy,
  KeyRound,
  Package,
  RefreshCw,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
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
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
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
import { useRouter, useSearchParams } from "../router";
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
  tier?: "basic" | "recommended" | "advanced";
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
    controlUrlEnv?: string;
    needsControlDeployToken?: boolean;
  };
  ssoReady: boolean;
  ui: {
    icon?: string;
    openMode?: "external" | "internal";
    dashboardPath?: string;
  };
  uiCapabilities?: string[];
  defaultImages?: Record<string, string>;
  presets: AddonPreset[];
  configSchema: AddonConfigField[];
  autoSecrets: AddonAutoSecret[];
  config: Record<string, string | number | boolean>;
  secretsStatus: Record<string, boolean>;
  canApplyInPlace: boolean;
};

type SavedRegistrySummary = {
  host: string;
  username: string;
  configured: boolean;
};

type SavedGitProviderSummary = {
  id: string;
  type: string;
  account: string;
  baseUrl: string;
  username: string;
  configured: boolean;
};

type AddonsPayload = {
  addons: AddonItem[];
  controlUrl: string;
  availableRegions: string[];
  availableNodes: string[];
  availableCredentials?: {
    registries: SavedRegistrySummary[];
    gitProviders: SavedGitProviderSummary[];
    defaultBuildRegistry?: { registryHost: string; pushHost: string };
  };
};

const CAPABILITY_LABELS: Record<string, [string, string]> = {
  "builder.git_import": ["从 Git 构建", "Git import"],
  "builder.runs": ["构建队列与重试", "Build queue"],
  "observe.apps": ["应用监控", "App monitoring"],
  "observe.logs": ["历史日志检索", "Log search"],
  "observe.traces": ["链路追踪", "Traces"],
  "observe.dashboards": ["Grafana 看板", "Grafana dashboards"],
  "observe.alerts": ["扩展告警规则", "Extended alerts"],
  "observe.status": ["采集健康", "Pipeline health"],
  "observe.settings": ["看板分享", "Dashboard sharing"],
  "eye.rum": ["前端埋点与崩溃", "Frontend RUM & crashes"],
};

function loc(text: LocalizedText | undefined, lang: Lang, fallback = ""): string {
  if (!text) return fallback;
  return (lang === "zh" ? text.zh || text.en : text.en || text.zh) || fallback;
}

function fieldTier(field: AddonConfigField): "basic" | "recommended" | "advanced" {
  if (field.tier === "basic" || field.tier === "recommended" || field.tier === "advanced") {
    return field.tier;
  }
  if (field.required || field.group === "placement") return "basic";
  return "advanced";
}

function AddonIcon({ icon }: { icon?: string }) {
  if (icon === "builder") return <Boxes className="size-4 text-primary" />;
  if (icon === "observe") return <Activity className="size-4 text-primary" />;
  return <Package className="size-4 text-primary" />;
}

export function AddonsPage({
  lang,
  token,
  onAddonsChange,
  onUnauthorized,
}: {
  lang: Lang;
  token: string;
  onAddonsChange?: () => void;
  onUnauthorized?: () => void;
}) {
  const zh = lang === "zh";
  const { path, navigate } = useRouter();
  const searchParams = useSearchParams();
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
  const [inheritRegistryHost, setInheritRegistryHost] = useState("");
  const [inheritGitIds, setInheritGitIds] = useState<string[]>([]);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [saving, setSaving] = useState<"" | "save" | "apply">("");
  const [saveError, setSaveError] = useState("");
  const [deployLogs, setDeployLogs] = useState<string[]>([]);
  const handledInstallParam = useRef("");

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

  const openEditor = useCallback((addon: AddonItem, creds?: AddonsPayload["availableCredentials"]) => {
    setEditing(addon);
    setPreset(addon.preset || addon.presets[0]?.id || "default");
    const nextCfg: Record<string, string> = {};
    for (const field of addon.configSchema) {
      if (field.type === "secret") continue;
      const val = addon.config?.[field.key];
      nextCfg[field.key] = val !== undefined && val !== null ? String(val) : String(field.default ?? "");
    }
    // For fresh builder setup, pre-select Control's default saved registry if available.
    let initialInheritReg = "";
    if (addon.id === "builder" && !nextCfg.registryHost && creds?.registries?.length) {
      const defaultHost = creds.defaultBuildRegistry?.registryHost;
      const match = creds.registries.find((r) => r.configured && r.host === defaultHost) || creds.registries.find((r) => r.configured);
      if (match) {
        initialInheritReg = match.host;
        nextCfg.registryHost = match.host;
        if (!nextCfg.registryUsername && match.username) nextCfg.registryUsername = match.username;
      }
    }
    setFormConfig(nextCfg);
    setFormSecrets({});
    setInheritRegistryHost(initialInheritReg);
    setInheritGitIds([]);
    setAdvancedOpen(false);
    setSaveError("");
    setDeployLogs([]);
  }, []);

  // Auto-open the install modal when navigated via /addons?install=<id>
  const installParam = searchParams.get("install") || "";
  useEffect(() => {
    if (!installParam || !data?.addons.length || handledInstallParam.current === installParam) return;
    const found = data.addons.find((item) => item.id === installParam);
    if (found) {
      handledInstallParam.current = installParam;
      openEditor(found, data.availableCredentials);
      navigate(path, { replace: true });
    }
  }, [installParam, data, openEditor, navigate, path]);

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

  const handleSelectInheritedRegistry = (host: string) => {
    setInheritRegistryHost(host);
    if (!host) return;
    const found = data?.availableCredentials?.registries.find((r) => r.host === host);
    if (found) {
      setFormConfig((prev) => ({
        ...prev,
        registryHost: found.host,
        registryUsername: found.username || prev.registryUsername || "",
      }));
    }
  };

  const toggleInheritGit = (providerId: string) => {
    setInheritGitIds((prev) =>
      prev.includes(providerId) ? prev.filter((id) => id !== providerId) : [...prev, providerId],
    );
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
    const inherit: { registryHost?: string; gitProviderIds?: string[] } = {};
    if (editing.id === "builder" && inheritRegistryHost) {
      inherit.registryHost = inheritRegistryHost;
    }
    if (editing.id === "builder" && inheritGitIds.length) {
      inherit.gitProviderIds = inheritGitIds;
    }
    return {
      preset,
      config: cfg,
      secrets,
      ...(Object.keys(inherit).length ? { inherit } : {}),
      origin: "dashboard",
    };
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
        setNotice(zh ? `${editing.name} 配置已保存并完成部署，对应控制台能力已激活。` : `${editing.name} configured and deployed. Its console capabilities are now active.`);
      } else {
        const res = await apiPost<{ generatedSecrets?: string[]; inheritedCredentials?: string[] }>(
          `/v1/addons/${encodeURIComponent(editing.id)}/configure`,
          token,
          payload,
        );
        const notes: string[] = [];
        if (res.generatedSecrets?.length) {
          notes.push(zh ? `自动生成密钥：${res.generatedSecrets.join(", ")}` : `auto-generated: ${res.generatedSecrets.join(", ")}`);
        }
        if (res.inheritedCredentials?.length) {
          notes.push(zh ? `已复用凭据：${res.inheritedCredentials.join(", ")}` : `reused: ${res.inheritedCredentials.join(", ")}`);
        }
        const suffix = notes.length ? (zh ? `（${notes.join("；")}）` : ` (${notes.join("; ")})`) : "";
        setNotice(zh ? `${editing.name} 配置已保存${suffix}。` : `${editing.name} configuration saved${suffix}.`);
      }
      await loadAddons();
      onAddonsChange?.();
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
      onAddonsChange?.();
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

  const renderField = (field: AddonConfigField) => {
    if (!editing) return null;
    const label = loc(field.label, lang, field.key);
    const desc = loc(field.description, lang);
    if (field.type === "secret") {
      const envName = field.env || "";
      const isInheritedRegPwd = editing.id === "builder" && envName === "LUMA_BUILD_REGISTRY_PASSWORD" && Boolean(inheritRegistryHost);
      const isSet = Boolean(editing.secretsStatus?.[envName]) || isInheritedRegPwd;
      return (
        <Field key={field.key}>
          <div className="flex items-center justify-between gap-2">
            <FieldLabel htmlFor={`field-${field.key}`}>{label}</FieldLabel>
            <Badge variant={isSet ? "secondary" : "outline"} className="text-[0.6875rem]">
              {isInheritedRegPwd
                ? zh ? "将复用 Control 凭据" : "Inherited from Control"
                : isSet
                  ? zh ? "已配置" : "Configured"
                  : zh ? "未设置" : "Unset"}
            </Badge>
          </div>
          <Input
            id={`field-${field.key}`}
            type="password"
            value={formSecrets[envName] || ""}
            onChange={(ev) => setFormSecrets((prev) => ({ ...prev, [envName]: ev.target.value }))}
            placeholder={
              isInheritedRegPwd
                ? zh ? "留空自动复用已选 Control 仓库密码" : "Leave blank to inherit selected Control registry password"
                : isSet
                  ? zh ? "留空保持现有密钥不变" : "Leave blank to keep existing secret"
                  : field.placeholder || ""
            }
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
  };

  const tieredFields = useMemo(() => {
    if (!editing) return { basic: [], recommended: [], advanced: [] };
    const basic: AddonConfigField[] = [];
    const recommended: AddonConfigField[] = [];
    const advanced: AddonConfigField[] = [];
    for (const f of editing.configSchema) {
      const tier = fieldTier(f);
      if (tier === "basic") basic.push(f);
      else if (tier === "recommended") recommended.push(f);
      else advanced.push(f);
    }
    return { basic, recommended, advanced };
  }, [editing]);

  const addons = data?.addons || [];
  const installedCount = addons.filter((a) => a.installed).length;
  const ssoCount = addons.filter((a) => a.ssoReady && a.installed).length;
  const savedRegistries = (data?.availableCredentials?.registries || []).filter((r) => r.configured);
  const savedGitProviders = (data?.availableCredentials?.gitProviders || []).filter((g) => g.configured);

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader
        meta={{
          eyebrow: zh ? "平台管理 / 插件中心" : "Platform / Addons",
          title: zh ? "插件中心" : "Addons",
          description: zh
            ? "按需启用可插拔官方扩展，安装后自动激活对应控制台能力与统一单点登录（Passkey 与 CLI Token），内部密钥全自动托管。"
            : "Enable optional extensions on demand. Installed addons automatically unlock their dashboard capabilities, unified Control SSO, and auto-managed internal secrets.",
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
          <Skeleton className="h-60 w-full rounded-xl" />
          <Skeleton className="h-60 w-full rounded-xl" />
          <Skeleton className="h-60 w-full rounded-xl" />
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {addons.map((addon) => {
            const installCmd = `luma addon install ${addon.id}`;
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
            const capLabels = (addon.uiCapabilities || [])
              .map((cap) => CAPABILITY_LABELS[cap]?.[zh ? 0 : 1])
              .filter(Boolean);

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

                <CardContent className="flex flex-col gap-3.5">
                  {capLabels.length > 0 ? (
                    <div className="flex flex-wrap gap-1">
                      {capLabels.slice(0, 5).map((label) => (
                        <Badge key={label} variant="secondary" className="text-[0.6875rem] font-normal">
                          {label}
                        </Badge>
                      ))}
                      {capLabels.length > 5 ? (
                        <Badge variant="outline" className="text-[0.6875rem] font-normal">
                          +{capLabels.length - 5}
                        </Badge>
                      ) : null}
                    </div>
                  ) : null}

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
                        {addon.url || (zh ? "安装时配置" : "Configured on install")}
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
                    <Button
                      size="sm"
                      variant={addon.installed ? "outline" : "default"}
                      onClick={() => openEditor(addon, data?.availableCredentials)}
                    >
                      <Settings className="mr-1 size-3.5" />
                      {addon.installed ? (zh ? "配置" : "Configure") : zh ? "一键安装" : "Install"}
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

      {/* 3-tier Schema-driven Configuration Dialog */}
      <Dialog open={Boolean(editing)} onOpenChange={(open) => { if (!open && !saving) setEditing(null); }}>
        <DialogContent className="max-h-[86dvh] overflow-y-auto sm:max-w-xl">
          {editing ? (
            <form
              onSubmit={(ev: FormEvent) => {
                ev.preventDefault();
                void handleSave(editing.canApplyInPlace && !editing.installed);
              }}
            >
              <DialogHeader>
                <DialogTitle>
                  {editing.installed
                    ? zh ? `配置 ${editing.name}` : `Configure ${editing.name}`
                    : zh ? `安装 ${editing.name}` : `Install ${editing.name}`}
                </DialogTitle>
                <DialogDescription>
                  {zh
                    ? "只需填写基础必填项；单点登录地址（LUMA_CONTROL_URL）与内部密钥由 Control 全自动生成与注入。"
                    : "Fill in only the required basics; SSO wiring (LUMA_CONTROL_URL) and internal secrets are auto-generated by Control."}
                </DialogDescription>
              </DialogHeader>

              <div className="my-4 flex flex-col gap-4">
                {saveError ? (
                  <Alert variant="destructive">
                    <TriangleAlert className="size-4" />
                    <AlertTitle>{zh ? "操作失败" : "Failed"}</AlertTitle>
                    <AlertDescription className="wrap-anywhere">{saveError}</AlertDescription>
                  </Alert>
                ) : null}

                {/* Zone 1: Basic & Required */}
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
                  {tieredFields.basic.map(renderField)}
                </FieldGroup>

                {/* Zone 2: Recommended + Control Credential Inheritance (Builder) */}
                {(tieredFields.recommended.length > 0 || (editing.id === "builder" && (savedRegistries.length > 0 || savedGitProviders.length > 0))) ? (
                  <div className="flex flex-col gap-3 rounded-lg border border-border bg-muted/20 p-3.5">
                    <div className="text-xs font-medium text-foreground">
                      {zh ? "推荐配置（镜像仓库与凭据复用）" : "Recommended (Registry & credential reuse)"}
                    </div>

                    {editing.id === "builder" && savedRegistries.length > 0 ? (
                      <Field>
                        <FieldLabel htmlFor="inherit-registry">
                          {zh ? "一键复用 Control 已保存的镜像仓库凭据" : "Reuse saved Control registry credential"}
                        </FieldLabel>
                        <select
                          id="inherit-registry"
                          value={inheritRegistryHost}
                          onChange={(ev) => handleSelectInheritedRegistry(ev.target.value)}
                          className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          <option value="">{zh ? "手动填写或保留现有配置" : "Enter manually or keep current"}</option>
                          {savedRegistries.map((r) => (
                            <option key={r.host} value={r.host}>
                              {r.host} ({r.username})
                            </option>
                          ))}
                        </select>
                        <FieldDescription>
                          {zh
                            ? "选择后将自动复用该仓库的地址、用户名和密码，无需重复输入密码。"
                            : "Automatically inherits the registry host, username, and password from Control."}
                        </FieldDescription>
                      </Field>
                    ) : null}

                    {editing.id === "builder" && savedGitProviders.length > 0 ? (
                      <div className="flex flex-col gap-1.5">
                        <span className="text-xs font-medium text-foreground">
                          {zh ? "复用 Control 已保存的 Git 凭据（可选）" : "Reuse saved Control Git credentials (optional)"}
                        </span>
                        <div className="flex flex-wrap gap-2">
                          {savedGitProviders.map((prov) => (
                            <label
                              key={prov.id}
                              className="flex cursor-pointer items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1 text-xs"
                            >
                              <input
                                type="checkbox"
                                checked={inheritGitIds.includes(prov.id)}
                                onChange={() => toggleInheritGit(prov.id)}
                              />
                              <span className="font-mono">{prov.type}:{prov.account}</span>
                            </label>
                          ))}
                        </div>
                      </div>
                    ) : null}

                    <FieldGroup className="gap-3">
                      {tieredFields.recommended.map(renderField)}
                    </FieldGroup>
                  </div>
                ) : null}

                {/* Zone 3: Auto-managed SSO & Internal Secrets */}
                <div className="rounded-lg border border-border bg-muted/35 p-3 text-xs">
                  <div className="mb-2 flex items-center gap-1.5 font-medium text-foreground">
                    <KeyRound className="size-3.5 text-emerald-600 dark:text-emerald-400" />
                    {zh ? "由 Luma Control 自动托管（无需配置）" : "Auto-managed by Luma Control (zero setup)"}
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {editing.auth.controlUrlEnv ? (
                      <Badge variant="secondary" className="font-mono text-[0.6875rem]">
                        {editing.auth.controlUrlEnv}: {zh ? "自动注入" : "auto-wired"}
                      </Badge>
                    ) : null}
                    {editing.auth.needsControlDeployToken ? (
                      <Badge variant="secondary" className="font-mono text-[0.6875rem]">
                        LUMA_DEPLOY_TOKEN: {zh ? "自动注入" : "auto-wired"}
                      </Badge>
                    ) : null}
                    {editing.auth.mode === "forward-auth" ? (
                      <Badge variant="secondary" className="font-mono text-[0.6875rem]">
                        /grafana ForwardAuth: {zh ? "自动保护" : "auto-protected"}
                      </Badge>
                    ) : null}
                    {editing.autoSecrets.map((s) => (
                      <Badge key={s.name} variant={editing.secretsStatus?.[s.name] ? "secondary" : "outline"} className="font-mono text-[0.6875rem]">
                        {s.name}: {editing.secretsStatus?.[s.name] ? (zh ? "就绪" : "ready") : zh ? "自动生成" : "auto-gen"}
                      </Badge>
                    ))}
                  </div>
                </div>

                {/* Zone 4: Collapsible Advanced & Optional Integrations */}
                {tieredFields.advanced.length > 0 ? (
                  <Collapsible open={advancedOpen} onOpenChange={setAdvancedOpen}>
                    <CollapsibleTrigger
                      render={
                        <Button type="button" variant="ghost" size="sm" className="w-full justify-between border border-border bg-background" />
                      }
                    >
                      <span className="flex items-center gap-1.5 text-xs font-medium">
                        <SlidersHorizontal className="size-3.5" aria-hidden="true" />
                        {zh
                          ? `可选集成与高级参数（${tieredFields.advanced.length} 项，均有默认值）`
                          : `Optional integrations & advanced (${tieredFields.advanced.length}, defaults applied)`}
                      </span>
                      <ChevronDown className={advancedOpen ? "size-3.5 rotate-180 transition-transform" : "size-3.5 transition-transform"} />
                    </CollapsibleTrigger>
                    <CollapsibleContent>
                      <div className="mt-3 rounded-lg border border-border bg-muted/15 p-3.5">
                        <FieldGroup className="gap-3.5">
                          {tieredFields.advanced.map(renderField)}
                        </FieldGroup>
                      </div>
                    </CollapsibleContent>
                  </Collapsible>
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
                <Button
                  type="button"
                  variant={editing.canApplyInPlace ? "outline" : "default"}
                  disabled={Boolean(saving)}
                  onClick={() => void handleSave(false)}
                >
                  {saving === "save" ? <Spinner className="mr-1.5 size-3.5" /> : null}
                  {zh ? "仅保存配置" : "Save config"}
                </Button>
                {editing.canApplyInPlace ? (
                  <Button type="button" disabled={Boolean(saving)} onClick={() => void handleSave(true)}>
                    {saving === "apply" ? <Spinner className="mr-1.5 size-3.5" /> : null}
                    {editing.installed
                      ? zh ? "保存并热更新部署" : "Save & redeploy"
                      : zh ? "保存并一键部署" : "Save & install"}
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
                ? "将停止并移除该插件服务及路由，立即吊销其 Luma Control 单点登录会话，并自动隐藏相关界面扩展槽。默认保留持久化数据卷。"
                : "Stops the addon services and routes, immediately revokes its Control SSO sessions, and hides its UI extension slots. Persistent data volumes are preserved."}
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
