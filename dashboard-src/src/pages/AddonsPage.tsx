import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  Activity,
  ArrowUpRight,
  Boxes,
  Check,
  CheckCircle2,
  ChevronDown,
  Copy,
  Cpu,
  Download,
  KeyRound,
  LayoutDashboard,
  Package,
  RefreshCw,
  RotateCcw,
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
import { Checkbox } from "@/components/ui/checkbox";
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
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { apiGet, apiPost, authHeaders, consumeNdjson } from "../apiClient";
import { BuilderRunnerOnboardingCard } from "../components/BuilderRunnerOnboardingCard";
import { fetchBuilderStatus, type BuilderStatus } from "../deploy/deployApi";
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

type AddonRequirements = {
  minMemoryMb?: number;
  recommendedMemoryMb?: number;
  minCpu?: number;
  stateful?: boolean;
  pinPolicy?: "required" | "recommended" | "manager" | "optional" | string;
  note?: LocalizedText;
  byPreset?: Record<string, { minMemoryMb?: number; recommendedMemoryMb?: number; minCpu?: number }>;
};

type AddonNodeDetail = {
  name: string;
  displayName: string;
  region: string;
  isManager: boolean;
  agentStatus: string;
  ready: boolean;
  memoryTotalMb: number;
  memoryAvailableMb: number;
  cpuCount: number;
  publicIngressReady: boolean;
};

type DashboardBundleRelease = {
  id: string;
  version: string;
  sha256: string;
  source: string;
  sourceUrl?: string;
  installedAt: number;
  fileCount: number;
  totalBytes: number;
  active?: boolean;
};

type DashboardBundleStatus = {
  mode: "bundle" | "embedded" | "bootstrap" | string;
  activeRelease: DashboardBundleRelease | null;
  embeddedAvailable: boolean;
  storeDir: string;
  releases: DashboardBundleRelease[];
};

type AddonItem = {
  id: string;
  name: string;
  kind?: "compose" | "static-bundle" | string;
  required?: boolean;
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
  suggestedDomain?: string;
  auth: {
    mode: "control-sso" | "forward-auth" | "builtin" | "none" | string;
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
  requirements?: AddonRequirements;
  effectiveRequirements?: AddonRequirements;
  defaultImages?: Record<string, string>;
  bundleStatus?: DashboardBundleStatus;
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
  managerNode?: string;
  availableRegions: string[];
  availableNodes: string[];
  nodeDetails?: AddonNodeDetail[];
  availableCredentials?: {
    registries: SavedRegistrySummary[];
    gitProviders: SavedGitProviderSummary[];
    defaultBuildRegistry?: { registryHost: string; pushHost: string };
  };
};

function fmtMemory(mb: number): string {
  if (!mb || mb <= 0) return "0 GiB";
  if (mb < 1024) return `${Math.round(mb)} MiB`;
  const gib = mb / 1024;
  const rounded = Math.round(gib * 10) / 10;
  return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)} GiB`;
}

function resolveEffectiveRequirements(addon: AddonItem, presetId: string): {
  minMemoryMb: number;
  recommendedMemoryMb: number;
  minCpu: number;
  stateful: boolean;
  pinPolicy: string;
  note?: LocalizedText;
} {
  const base = addon.requirements || addon.effectiveRequirements || {};
  const override = base.byPreset?.[presetId];
  return {
    minMemoryMb: override?.minMemoryMb ?? base.minMemoryMb ?? 0,
    recommendedMemoryMb: override?.recommendedMemoryMb ?? base.recommendedMemoryMb ?? 0,
    minCpu: override?.minCpu ?? base.minCpu ?? 0,
    stateful: Boolean(base.stateful),
    pinPolicy: base.pinPolicy || "optional",
    note: base.note,
  };
}

function sortNodesForAddon(
  nodes: AddonNodeDetail[],
  opts: { addonId: string; presetId: string; exposure: string; managerNode: string },
): AddonNodeDetail[] {
  const preferIngress = opts.addonId === "eye" && (opts.presetId === "node-edge" || opts.exposure === "node-edge");
  return [...nodes].sort((a, b) => {
    if (a.ready !== b.ready) return a.ready ? -1 : 1;
    if (preferIngress && a.publicIngressReady !== b.publicIngressReady) {
      return a.publicIngressReady ? -1 : 1;
    }
    if (opts.addonId === "builder" && opts.managerNode) {
      if (a.name === opts.managerNode && b.name !== opts.managerNode) return -1;
      if (b.name === opts.managerNode && a.name !== opts.managerNode) return 1;
    }
    if (a.memoryAvailableMb !== b.memoryAvailableMb) return b.memoryAvailableMb - a.memoryAvailableMb;
    if (a.memoryTotalMb !== b.memoryTotalMb) return b.memoryTotalMb - a.memoryTotalMb;
    return a.name.localeCompare(b.name);
  });
}

function pickBestNode(
  nodeDetails: AddonNodeDetail[],
  opts: { region: string; addonId: string; presetId: string; exposure: string; managerNode: string },
): string {
  const inRegion = nodeDetails.filter((n) => n.region === opts.region);
  if (inRegion.length === 0) return "";
  if (inRegion.length === 1) return inRegion[0].name;
  const sorted = sortNodesForAddon(inRegion, opts);
  return sorted[0]?.name || "";
}

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
  if (icon === "dashboard") return <LayoutDashboard className="size-4 text-primary" />;
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

  // Live builder runner check (non-blocking, only when Luma Build is installed)
  const [builderStatus, setBuilderStatus] = useState<BuilderStatus | null>(null);
  const [recheckingRunners, setRecheckingRunners] = useState(false);

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
  const [justDeployedAddonId, setJustDeployedAddonId] = useState("");
  const handledInstallParam = useRef("");

  // Uninstall confirmation dialog state
  const [removing, setRemoving] = useState<AddonItem | null>(null);
  const [purgeSecrets, setPurgeSecrets] = useState(false);
  const [uninstalling, setUninstalling] = useState(false);

  // Static-bundle hot-swap state (e.g. Luma Dashboard) — stays inline on buttons to avoid layout shifts
  const [bundleOp, setBundleOp] = useState<{
    addonId: string;
    addonName: string;
    action: "update" | "upload" | "rollback";
    status: "running" | "success" | "error";
    error?: string;
    logs: string[];
  } | null>(null);
  const [bundleLogOpen, setBundleLogOpen] = useState(false);
  const bundleResetTimerRef = useRef<number | null>(null);
  const bundleFileInputRef = useRef<HTMLInputElement | null>(null);
  const [uploadTargetAddon, setUploadTargetAddon] = useState<AddonItem | null>(null);

  const clearBundleResetTimer = useCallback(() => {
    if (bundleResetTimerRef.current !== null) {
      window.clearTimeout(bundleResetTimerRef.current);
      bundleResetTimerRef.current = null;
    }
  }, []);

  useEffect(() => clearBundleResetTimer, [clearBundleResetTimer]);

  const loadBuilderStatus = useCallback(async () => {
    setRecheckingRunners(true);
    try {
      const next = await fetchBuilderStatus(token);
      setBuilderStatus(next);
    } catch {
      // Builder may still be starting or temporarily unreachable; stay non-blocking.
      setBuilderStatus(null);
    } finally {
      setRecheckingRunners(false);
    }
  }, [token]);

  const loadAddons = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const payload = await apiGet<AddonsPayload>("/v1/addons", token);
      setData(payload);
      const builderItem = payload.addons.find((item) => item.id === "builder");
      if (builderItem?.installed) {
        void loadBuilderStatus();
      } else {
        setBuilderStatus(null);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.toLowerCase().includes("unauthorized") || msg.includes("401")) {
        onUnauthorized?.();
      }
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [token, onUnauthorized, loadBuilderStatus]);

  useEffect(() => {
    void loadAddons();
  }, [loadAddons]);

  const openEditor = useCallback((addon: AddonItem, payload?: AddonsPayload | null) => {
    const creds = payload?.availableCredentials;
    const nodeDetails = payload?.nodeDetails || [];
    const managerNode = payload?.managerNode || "";
    const initialPreset = addon.preset || addon.presets[0]?.id || "default";
    setEditing(addon);
    setPreset(initialPreset);
    const nextCfg: Record<string, string> = {};
    for (const field of addon.configSchema) {
      if (field.type === "secret") continue;
      const val = addon.config?.[field.key];
      nextCfg[field.key] = val !== undefined && val !== null ? String(val) : String(field.default ?? "");
    }
    if (!addon.installed && !addon.configured && !nextCfg.domain && addon.suggestedDomain) {
      nextCfg.domain = addon.suggestedDomain;
    }
    // Smart stateful node pre-selection when installing fresh or when current node does not match region.
    const hasNodeField = addon.configSchema.some((f) => f.type === "node");
    if (hasNodeField && nodeDetails.length > 0 && nextCfg.region) {
      const nodeInRegion = nodeDetails.some((n) => n.name === nextCfg.node && n.region === nextCfg.region);
      if (!nextCfg.node || (!addon.installed && !nodeInRegion)) {
        const picked = pickBestNode(nodeDetails, {
          region: nextCfg.region,
          addonId: addon.id,
          presetId: initialPreset,
          exposure: nextCfg.exposure || "",
          managerNode,
        });
        if (picked) nextCfg.node = picked;
      }
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
    setJustDeployedAddonId("");
  }, []);

  // Auto-open the install modal when navigated via /addons?install=<id>
  const installParam = searchParams.get("install") || "";
  useEffect(() => {
    if (!installParam || !data?.addons.length || handledInstallParam.current === installParam) return;
    const found = data.addons.find((item) => item.id === installParam);
    if (found) {
      handledInstallParam.current = installParam;
      openEditor(found, data);
      navigate(path, { replace: true });
    }
  }, [installParam, data, openEditor, navigate, path]);

  const handlePresetChange = (nextPreset: string) => {
    setPreset(nextPreset);
    if (!editing) return;
    const found = editing.presets.find((item) => item.id === nextPreset);
    if (!found) return;
    const nodeDetails = data?.nodeDetails || [];
    const managerNode = data?.managerNode || "";
    setFormConfig((prev) => {
      let nextRegion = found.defaultRegion || prev.region || "";
      let nextExposure = found.defaultExposure || prev.exposure || "";
      if (nodeDetails.length > 0 && nextRegion) {
        const hasNodesInDefault = nodeDetails.some((n) => n.region === nextRegion);
        if (!hasNodesInDefault) {
          if (editing.id === "eye" && nextPreset === "node-edge") {
            const pubNode = nodeDetails.find((n) => n.publicIngressReady && n.region);
            if (pubNode) nextRegion = pubNode.region;
          }
          if (!nodeDetails.some((n) => n.region === nextRegion)) {
            const mgr = nodeDetails.find((n) => n.isManager && n.region);
            nextRegion = mgr?.region || nodeDetails[0]?.region || nextRegion;
          }
        }
        if (nextRegion !== "cn" && nextExposure === "cn-edge") {
          nextExposure = "external-edge";
        }
      }
      let nextNode = prev.node || found.defaultNode || "";
      const hasNodeField = editing.configSchema.some((f) => f.type === "node");
      if (hasNodeField && nodeDetails.length > 0 && nextRegion) {
        const stillValid = nodeDetails.some((n) => n.name === nextNode && n.region === nextRegion);
        if (!nextNode || !stillValid) {
          nextNode = pickBestNode(nodeDetails, {
            region: nextRegion,
            addonId: editing.id,
            presetId: nextPreset,
            exposure: nextExposure,
            managerNode,
          });
        }
      }
      return {
        ...prev,
        ...(nextRegion ? { region: nextRegion } : {}),
        ...(nextExposure ? { exposure: nextExposure } : {}),
        ...(hasNodeField ? { node: nextNode } : {}),
      };
    });
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
        setJustDeployedAddonId(editing.id);
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

  const scheduleBundleSuccessReset = useCallback(() => {
    clearBundleResetTimer();
    bundleResetTimerRef.current = window.setTimeout(() => {
      bundleResetTimerRef.current = null;
      setBundleOp((prev) => (prev?.status === "success" ? null : prev));
    }, 3000);
  }, [clearBundleResetTimer]);

  const handleBundleHotSwap = async (addon: AddonItem) => {
    clearBundleResetTimer();
    setBundleLogOpen(false);
    const collectedLogs: string[] = [];
    setBundleOp({
      addonId: addon.id,
      addonName: addon.name,
      action: "update",
      status: "running",
      logs: [],
    });
    try {
      const response = await fetch(`/v1/addons/${encodeURIComponent(addon.id)}/apply/stream`, {
        method: "POST",
        headers: authHeaders(token, true),
        body: JSON.stringify({ origin: "dashboard" }),
      });
      await consumeNdjson(response, (ev) => {
        const name = String(ev.name || ev.stage || "");
        const msg = String(ev.message || "");
        if (name || msg) {
          collectedLogs.push(`[${String(ev.status || "step")}] ${name}${msg ? `: ${msg}` : ""}`);
        }
      });
      setBundleOp({
        addonId: addon.id,
        addonName: addon.name,
        action: "update",
        status: "success",
        logs: collectedLogs,
      });
      scheduleBundleSuccessReset();
      await loadAddons();
      onAddonsChange?.();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      collectedLogs.push(`[fail] ${msg}`);
      setBundleOp({
        addonId: addon.id,
        addonName: addon.name,
        action: "update",
        status: "error",
        error: msg,
        logs: collectedLogs,
      });
    }
  };

  const handleBundleUpload = async (addon: AddonItem, file: File) => {
    clearBundleResetTimer();
    setBundleLogOpen(false);
    const collectedLogs: string[] = [`[running] Upload ${file.name} (${(file.size / 1024).toFixed(1)} KiB)`];
    setBundleOp({
      addonId: addon.id,
      addonName: addon.name,
      action: "upload",
      status: "running",
      logs: collectedLogs,
    });
    try {
      const buffer = await file.arrayBuffer();
      const params = new URLSearchParams({ source: "browser" });
      const response = await fetch(`/v1/addons/${encodeURIComponent(addon.id)}/upload?${params.toString()}`, {
        method: "POST",
        headers: {
          ...authHeaders(token),
          "Content-Type": "application/gzip",
        },
        body: buffer,
      });
      const text = await response.text();
      const parsed = text ? (JSON.parse(text) as { error?: string; release?: DashboardBundleRelease }) : {};
      if (!response.ok) {
        throw new Error(parsed.error || `HTTP ${response.status}`);
      }
      collectedLogs.push(`[ok] Activated ${parsed.release?.version || file.name}`);
      setBundleOp({
        addonId: addon.id,
        addonName: addon.name,
        action: "upload",
        status: "success",
        logs: collectedLogs,
      });
      scheduleBundleSuccessReset();
      await loadAddons();
      onAddonsChange?.();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      collectedLogs.push(`[fail] ${msg}`);
      setBundleOp({
        addonId: addon.id,
        addonName: addon.name,
        action: "upload",
        status: "error",
        error: msg,
        logs: collectedLogs,
      });
    } finally {
      setUploadTargetAddon(null);
      if (bundleFileInputRef.current) bundleFileInputRef.current.value = "";
    }
  };

  const handleBundleRollback = async (addon: AddonItem, releaseId?: string) => {
    clearBundleResetTimer();
    setBundleLogOpen(false);
    const collectedLogs: string[] = [`[running] Rollback ${addon.name}${releaseId ? ` -> ${releaseId}` : ""}`];
    setBundleOp({
      addonId: addon.id,
      addonName: addon.name,
      action: "rollback",
      status: "running",
      logs: collectedLogs,
    });
    try {
      const res = await apiPost<{ release?: DashboardBundleRelease }>(
        `/v1/addons/${encodeURIComponent(addon.id)}/rollback`,
        token,
        releaseId ? { releaseId } : {},
      );
      collectedLogs.push(`[ok] Activated ${res.release?.version || res.release?.id || "previous release"}`);
      setBundleOp({
        addonId: addon.id,
        addonName: addon.name,
        action: "rollback",
        status: "success",
        logs: collectedLogs,
      });
      scheduleBundleSuccessReset();
      await loadAddons();
      onAddonsChange?.();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      collectedLogs.push(`[fail] ${msg}`);
      setBundleOp({
        addonId: addon.id,
        addonName: addon.name,
        action: "rollback",
        status: "error",
        error: msg,
        logs: collectedLogs,
      });
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

    if (field.type === "node") {
      const nodeDetails = data?.nodeDetails || [];
      const selectedRegion = formConfig.region || "";
      const managerNode = data?.managerNode || "";
      const rawInRegion = selectedRegion
        ? nodeDetails.filter((n) => n.region === selectedRegion)
        : nodeDetails;
      const sortedRegionNodes = sortNodesForAddon(rawInRegion, {
        addonId: editing.id,
        presetId: preset,
        exposure: formConfig.exposure || "",
        managerNode,
      });
      const currentVal = formConfig[field.key] ?? "";
      const reqs = resolveEffectiveRequirements(editing, preset);
      const isRequiredPin = Boolean(field.required || reqs.pinPolicy === "required");
      const isRecommendedPin = !isRequiredPin && Boolean(reqs.stateful || reqs.pinPolicy === "recommended");
      const currentOutsideRegion = Boolean(
        currentVal && nodeDetails.length > 0 && !sortedRegionNodes.some((n) => n.name === currentVal),
      );
      const fallbackNames = data?.availableNodes || [];
      const movingStatefulNode = Boolean(
        editing.installed &&
          reqs.stateful &&
          editing.config?.node &&
          currentVal &&
          String(editing.config.node) !== currentVal,
      );

      const nodeOptions: Array<{ value: string; label: string }> = [];
      if (!isRequiredPin) {
        nodeOptions.push({
          value: "__auto__",
          label: zh ? "自动调度（不固定节点）" : "Auto-schedule (unpinned)",
        });
      } else if (sortedRegionNodes.length === 0 && fallbackNames.length === 0) {
        nodeOptions.push({
          value: "__empty__",
          label: zh ? "当前区域暂无可用节点" : "No nodes available in this region",
        });
      }
      if (currentOutsideRegion) {
        nodeOptions.push({
          value: currentVal,
          label: `${currentVal} (${zh ? "当前不在所选区域" : "outside selected region"})`,
        });
      }
      if (sortedRegionNodes.length > 0) {
        for (const n of sortedRegionNodes) {
          const memText =
            n.memoryTotalMb > 0
              ? zh
                ? `${fmtMemory(n.memoryAvailableMb)} / ${fmtMemory(n.memoryTotalMb)} 可用`
                : `${fmtMemory(n.memoryAvailableMb)} / ${fmtMemory(n.memoryTotalMb)} free`
              : "";
          const tags = [
            memText,
            n.isManager ? "Manager" : "",
            n.publicIngressReady ? (zh ? "公网直连就绪" : "node-edge ready") : "",
            !n.ready ? (zh ? "离线" : "offline") : "",
          ]
            .filter(Boolean)
            .join(" · ");
          nodeOptions.push({
            value: n.name,
            label: tags ? `${n.name} — ${tags}` : n.name,
          });
        }
      } else {
        for (const opt of fallbackNames) {
          nodeOptions.push({ value: opt, label: opt });
        }
      }
      const selectNodeValue =
        currentVal || (!isRequiredPin ? "__auto__" : nodeOptions[0]?.value === "__empty__" ? "__empty__" : "");

      return (
        <Field key={field.key} className="min-w-0">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <FieldLabel htmlFor={`field-${field.key}`}>{label}</FieldLabel>
            {isRequiredPin ? (
              <Badge variant="secondary" className="text-[0.6875rem]">
                {zh ? "有状态数据卷 · 必须固定节点" : "Stateful volumes · Pin required"}
              </Badge>
            ) : isRecommendedPin ? (
              <Badge variant="outline" className="text-[0.6875rem]">
                {zh ? "有状态数据卷 · 建议固定节点" : "Stateful volume · Pin recommended"}
              </Badge>
            ) : null}
          </div>
          <Select
            items={nodeOptions}
            value={selectNodeValue}
            onValueChange={(next) => {
              const resolved = !next || next === "__auto__" || next === "__empty__" ? "" : next;
              setFormConfig((prev) => ({ ...prev, [field.key]: resolved }));
            }}
          >
            <SelectTrigger id={`field-${field.key}`} className="w-full min-w-0">
              <SelectValue placeholder={zh ? "请选择固定节点…" : "Select a pinned node…"} />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false}>
              <SelectGroup>
                {nodeOptions.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value} disabled={opt.value === "__empty__"}>
                    {opt.label}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
          {sortedRegionNodes.length === 1 && currentVal === sortedRegionNodes[0].name ? (
            <FieldDescription>
              {zh
                ? `目标区域 ${selectedRegion || ""} 仅有 1 台节点，已自动选中 ${sortedRegionNodes[0].name}。`
                : `Only 1 node exists in region ${selectedRegion || ""}; automatically selected ${sortedRegionNodes[0].name}.`}
            </FieldDescription>
          ) : sortedRegionNodes.length > 1 && (isRequiredPin || isRecommendedPin) ? (
            <FieldDescription>
              {zh
                ? `目标区域有 ${sortedRegionNodes.length} 台节点。${isRequiredPin ? "该插件包含本地有状态卷，必须固定节点；已按资源与入口自动预选最合适节点。" : "该插件含本地有状态卷，建议固定节点；已自动预选最合适节点。"}`
                : `${sortedRegionNodes.length} nodes in target region. ${isRequiredPin ? "Stateful local volumes require a pinned node; best candidate pre-selected." : "Pinning is recommended for local volumes; best candidate pre-selected."}`}
            </FieldDescription>
          ) : desc ? (
            <FieldDescription>{desc}</FieldDescription>
          ) : null}
          {movingStatefulNode ? (
            <p className="text-xs font-medium text-amber-600 dark:text-amber-400">
              {zh
                ? `注意：原数据卷位于节点 ${String(editing.config.node)}，切换到 ${currentVal} 不会自动迁移旧节点上的本地历史数据。`
                : `Warning: existing local volumes live on ${String(editing.config.node)}. Switching to ${currentVal} does not migrate old local data.`}
            </p>
          ) : null}
        </Field>
      );
    }

    if (field.type === "select" || field.type === "region") {
      const options =
        field.type === "select"
          ? field.options || []
          : data?.availableRegions || ["cn", "global", "home"];
      const selectItems = options.map((opt) => ({ value: opt, label: opt }));
      return (
        <Field key={field.key} className="min-w-0">
          <FieldLabel htmlFor={`field-${field.key}`}>{label}</FieldLabel>
          <Select
            items={selectItems}
            value={formConfig[field.key] ?? ""}
            onValueChange={(nextVal) => {
              if (nextVal === null) return;
              setFormConfig((prev) => {
                const updated = { ...prev, [field.key]: nextVal };
                if (field.type === "region") {
                  const hasNodeField = editing.configSchema.some((f) => f.type === "node");
                  const nodeDetails = data?.nodeDetails || [];
                  if (hasNodeField && nodeDetails.length > 0) {
                    const stillInRegion = nodeDetails.some((n) => n.name === prev.node && n.region === nextVal);
                    if (!prev.node || !stillInRegion) {
                      updated.node = pickBestNode(nodeDetails, {
                        region: nextVal,
                        addonId: editing.id,
                        presetId: preset,
                        exposure: prev.exposure || "",
                        managerNode: data?.managerNode || "",
                      });
                    }
                  }
                }
                return updated;
              });
            }}
          >
            <SelectTrigger id={`field-${field.key}`} className="w-full min-w-0">
              <SelectValue />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false}>
              <SelectGroup>
                {selectItems.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    {opt.label}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
          {desc ? <FieldDescription>{desc}</FieldDescription> : null}
        </Field>
      );
    }

    if (field.type === "boolean") {
      const checked = String(formConfig[field.key] ?? "").toLowerCase() === "true";
      return (
        <Field key={field.key} className="min-w-0">
          <div className="flex items-center justify-between gap-3">
            <FieldLabel htmlFor={`field-${field.key}`}>{label}</FieldLabel>
            <Switch
              id={`field-${field.key}`}
              checked={checked}
              onCheckedChange={(val) => setFormConfig((prev) => ({ ...prev, [field.key]: val ? "true" : "false" }))}
            />
          </div>
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
  const onlineBuilderRunners = useMemo(
    () => (builderStatus?.runners || []).filter((r) => Boolean(r.online) && !r.revoked),
    [builderStatus],
  );

  const resourcePrecheck = useMemo(() => {
    if (!editing) return null;
    const reqs = resolveEffectiveRequirements(editing, preset);
    if (!reqs.minMemoryMb && !reqs.recommendedMemoryMb) return null;
    const nodeDetails = data?.nodeDetails || [];
    const targetNode =
      reqs.pinPolicy === "manager"
        ? nodeDetails.find((n) => n.isManager) || nodeDetails.find((n) => n.name === data?.managerNode)
        : nodeDetails.find((n) => n.name === formConfig.node);
    if (!targetNode) {
      return {
        level: "info" as const,
        reqs,
        targetNode: null,
        sameNodeRedeploy: false,
      };
    }
    const sameNodeRedeploy = Boolean(
      editing.installed &&
        (reqs.pinPolicy === "manager" || String(editing.config?.node || "") === targetNode.name),
    );
    const total = targetNode.memoryTotalMb;
    const avail = targetNode.memoryAvailableMb;
    let level: "ok" | "warn" | "danger" | "info" = "info";
    if (total > 0) {
      if (sameNodeRedeploy) {
        if (reqs.minMemoryMb && total < reqs.minMemoryMb) level = "danger";
        else if (reqs.recommendedMemoryMb && total < reqs.recommendedMemoryMb) level = "warn";
        else level = "ok";
      } else {
        if (reqs.minMemoryMb && (avail < reqs.minMemoryMb || total < reqs.minMemoryMb)) level = "danger";
        else if (reqs.recommendedMemoryMb && (avail < reqs.recommendedMemoryMb || total < reqs.recommendedMemoryMb)) level = "warn";
        else level = "ok";
      }
    }
    return { level, reqs, targetNode, sameNodeRedeploy };
  }, [editing, preset, data, formConfig.node]);

  const missingRequiredNode = Boolean(
    editing &&
      editing.configSchema.some((f) => f.type === "node" && (f.required || editing.requirements?.pinPolicy === "required")) &&
      !(formConfig.node || "").trim(),
  );

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

      <input
        ref={bundleFileInputRef}
        type="file"
        accept=".tar.gz,.tgz,.tar,application/gzip,application/x-gzip"
        className="hidden"
        onChange={(ev) => {
          const file = ev.target.files?.[0];
          if (file && uploadTargetAddon) {
            void handleBundleUpload(uploadTargetAddon, file);
          }
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
            const isStaticBundle = addon.kind === "static-bundle";
            const isRequired = Boolean(addon.required);
            const bundleStatus = addon.bundleStatus;
            const activeRelease = bundleStatus?.activeRelease || null;
            const releaseCount = bundleStatus?.releases?.length || 0;
            const installCmd = `luma addon install ${addon.id}`;
            const authLabel =
              addon.auth.mode === "control-sso"
                ? "Control SSO"
                : addon.auth.mode === "forward-auth"
                  ? "ForwardAuth"
                  : addon.auth.mode === "builtin"
                    ? zh
                      ? "原生会话"
                      : "Native session"
                    : zh
                      ? "独立认证"
                      : "Standalone";
            const bundleModeLabel =
              bundleStatus?.mode === "bundle"
                ? zh
                  ? "静态热更新包"
                  : "Hot-swapped bundle"
                : bundleStatus?.mode === "embedded"
                  ? zh
                    ? "内置回退"
                    : "Embedded fallback"
                  : zh
                    ? "待拉取"
                    : "Bootstrap";
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
            const cardReqs = resolveEffectiveRequirements(addon, addon.preset);
            const pinBadgeText =
              cardReqs.pinPolicy === "required"
                ? zh ? "必须固定节点" : "Pin required"
                : cardReqs.pinPolicy === "manager"
                  ? zh ? "固定 Manager" : "Pinned to Manager"
                  : cardReqs.pinPolicy === "recommended"
                    ? zh ? "建议固定节点" : "Pin recommended"
                    : "";

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
                    <div className="flex items-center gap-1.5">
                      {isRequired ? (
                        <Badge variant="secondary" className="text-[0.6875rem]">
                          {zh ? "核心必选" : "Core"}
                        </Badge>
                      ) : null}
                      <Badge variant={statusVariant}>{statusText}</Badge>
                    </div>
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
                    {isStaticBundle ? (
                      <>
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-muted-foreground">{zh ? "交付方式" : "Delivery"}</span>
                          <span className="font-medium text-foreground">{bundleModeLabel}</span>
                        </div>
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-muted-foreground">{zh ? "当前版本" : "Active release"}</span>
                          <span
                            className="max-w-[190px] truncate font-mono text-foreground"
                            title={activeRelease ? `${activeRelease.version} (${activeRelease.sha256})` : bundleModeLabel}
                          >
                            {activeRelease
                              ? `${activeRelease.version} · ${activeRelease.sha256.slice(0, 8)}`
                              : zh
                                ? "内置版本"
                                : "Bundled"}
                          </span>
                        </div>
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-muted-foreground">{zh ? "托管目录" : "Store path"}</span>
                          <span className="max-w-[190px] truncate font-mono text-foreground" title={bundleStatus?.storeDir || "/opt/luma/dashboard"}>
                            {bundleStatus?.storeDir || "/opt/luma/dashboard"}
                          </span>
                        </div>
                        {releaseCount > 0 ? (
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-muted-foreground">{zh ? "可回滚历史" : "Rollback cache"}</span>
                            <span className="font-medium text-foreground">
                              {zh ? `${releaseCount} 个版本` : `${releaseCount} release(s)`}
                            </span>
                          </div>
                        ) : null}
                      </>
                    ) : (
                      <>
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-muted-foreground">{zh ? "部署预设" : "Preset"}</span>
                          <span className="font-mono text-foreground">
                            {addon.preset}
                            {addon.config?.node ? ` · ${String(addon.config.node)}` : ""}
                          </span>
                        </div>
                        {cardReqs.recommendedMemoryMb > 0 ? (
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-muted-foreground">{zh ? "建议资源" : "Resources"}</span>
                            <span className="text-right font-medium text-foreground">
                              {zh ? `≥ ${fmtMemory(cardReqs.recommendedMemoryMb)} 内存` : `>= ${fmtMemory(cardReqs.recommendedMemoryMb)} RAM`}
                              {pinBadgeText ? ` · ${pinBadgeText}` : ""}
                            </span>
                          </div>
                        ) : null}
                      </>
                    )}
                    {addon.id === "builder" && addon.installed && builderStatus ? (
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-muted-foreground">{zh ? "在线 Runner" : "Online runners"}</span>
                        <span
                          className={
                            onlineBuilderRunners.length > 0
                              ? "font-medium text-emerald-600 dark:text-emerald-400"
                              : "font-medium text-amber-600 dark:text-amber-400"
                          }
                        >
                          {onlineBuilderRunners.length > 0
                            ? zh
                              ? `${onlineBuilderRunners.length} 台在线`
                              : `${onlineBuilderRunners.length} online`
                            : zh
                              ? "0 台（待接入）"
                              : "0 (action needed)"}
                        </span>
                      </div>
                    ) : null}
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-muted-foreground">{zh ? "访问入口" : "Endpoint"}</span>
                      <span className="max-w-[190px] truncate font-mono text-foreground" title={addon.consoleUrl || addon.url || "-"}>
                        {addon.url || (zh ? "安装时配置" : "Configured on install")}
                      </span>
                    </div>
                  </div>

                  {addon.id === "builder" && addon.installed && builderStatus && onlineBuilderRunners.length === 0 ? (
                    <BuilderRunnerOnboardingCard
                      compact
                      lang={lang}
                      builderUrl={addon.url || addon.consoleUrl}
                      rechecking={recheckingRunners}
                      onRecheck={() => void loadBuilderStatus()}
                    />
                  ) : null}

                  {!addon.installed ? (
                    <div className="flex items-center justify-between gap-2 rounded-lg border border-dashed border-border bg-background px-2.5 py-1 font-mono text-[0.7rem] text-muted-foreground">
                      <span className="truncate">{installCmd}</span>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-xs"
                        onClick={() => void copyCommand(addon.id, installCmd)}
                        className="shrink-0 text-muted-foreground hover:text-foreground"
                        aria-label={zh ? "复制安装命令" : "Copy install command"}
                      >
                        {copiedId === addon.id ? <Check className="size-3.5 text-emerald-600" /> : <Copy className="size-3.5" />}
                      </Button>
                    </div>
                  ) : null}

                  <div className="flex flex-wrap items-center gap-2">
                    {isStaticBundle ? (() => {
                      const activeOp = bundleOp && bundleOp.addonId === addon.id ? bundleOp : null;
                      const isRunning = activeOp?.status === "running";
                      const updateState = activeOp?.action === "update" ? activeOp.status : null;
                      const uploadState = activeOp?.action === "upload" ? activeOp.status : null;
                      const rollbackState = activeOp?.action === "rollback" ? activeOp.status : null;
                      return (
                        <>
                          <Button
                            size="sm"
                            variant={updateState === "error" ? "destructive" : "default"}
                            disabled={isRunning}
                            onClick={() => {
                              if (updateState === "error") {
                                setBundleLogOpen(true);
                                return;
                              }
                              void handleBundleHotSwap(addon);
                            }}
                          >
                            {updateState === "running" ? (
                              <Spinner className="mr-1 size-3.5" />
                            ) : updateState === "success" ? (
                              <Check className="mr-1 size-3.5" />
                            ) : updateState === "error" ? (
                              <TriangleAlert className="mr-1 size-3.5" />
                            ) : (
                              <Download className="mr-1 size-3.5" />
                            )}
                            {updateState === "running"
                              ? zh ? "更新中…" : "Updating…"
                              : updateState === "success"
                                ? zh ? "已更新" : "Updated"
                                : updateState === "error"
                                  ? zh ? "查看日志" : "View logs"
                                  : zh ? "在线热更新" : "Hot-swap update"}
                          </Button>
                          <Button
                            size="sm"
                            variant={uploadState === "error" ? "destructive" : "outline"}
                            disabled={isRunning}
                            onClick={() => {
                              if (uploadState === "error") {
                                setBundleLogOpen(true);
                                return;
                              }
                              setUploadTargetAddon(addon);
                              bundleFileInputRef.current?.click();
                            }}
                          >
                            {uploadState === "running" ? (
                              <Spinner className="mr-1 size-3.5" />
                            ) : uploadState === "success" ? (
                              <Check className="mr-1 size-3.5 text-emerald-600 dark:text-emerald-400" />
                            ) : uploadState === "error" ? (
                              <TriangleAlert className="mr-1 size-3.5" />
                            ) : (
                              <Package className="mr-1 size-3.5" />
                            )}
                            {uploadState === "running"
                              ? zh ? "上传中…" : "Uploading…"
                              : uploadState === "success"
                                ? zh ? "已上传" : "Uploaded"
                                : uploadState === "error"
                                  ? zh ? "查看日志" : "View logs"
                                  : zh ? "上传本地包" : "Upload bundle"}
                          </Button>
                          {releaseCount > 1 ? (
                            <Button
                              size="sm"
                              variant={rollbackState === "error" ? "destructive" : "outline"}
                              disabled={isRunning}
                              onClick={() => {
                                if (rollbackState === "error") {
                                  setBundleLogOpen(true);
                                  return;
                                }
                                void handleBundleRollback(addon);
                              }}
                            >
                              {rollbackState === "running" ? (
                                <Spinner className="mr-1 size-3.5" />
                              ) : rollbackState === "success" ? (
                                <Check className="mr-1 size-3.5 text-emerald-600 dark:text-emerald-400" />
                              ) : rollbackState === "error" ? (
                                <TriangleAlert className="mr-1 size-3.5" />
                              ) : (
                                <RotateCcw className="mr-1 size-3.5" />
                              )}
                              {rollbackState === "running"
                                ? zh ? "回滚中…" : "Rolling back…"
                                : rollbackState === "success"
                                  ? zh ? "已回滚" : "Rolled back"
                                  : rollbackState === "error"
                                    ? zh ? "查看日志" : "View logs"
                                    : zh ? "回滚" : "Rollback"}
                            </Button>
                          ) : null}
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={isRunning}
                            onClick={() => openEditor(addon, data)}
                          >
                            <Settings className="mr-1 size-3.5" />
                            {zh ? "配置源" : "Source"}
                          </Button>
                        </>
                      );
                    })() : (
                      <>
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
                          onClick={() => openEditor(addon, data)}
                        >
                          <Settings className="mr-1 size-3.5" />
                          {addon.installed ? (zh ? "配置" : "Configure") : zh ? "一键安装" : "Install"}
                        </Button>
                        {addon.installed && !isRequired ? (
                          <Button
                            size="icon-sm"
                            variant="ghost"
                            className="ml-auto text-destructive hover:text-destructive"
                            aria-label={zh ? `卸载 ${addon.name}` : `Uninstall ${addon.name}`}
                            onClick={() => {
                              setRemoving(addon);
                              setPurgeSecrets(false);
                            }}
                          >
                            <Trash2 className="size-3.5" />
                          </Button>
                        ) : null}
                      </>
                    )}
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {/* 3-tier Schema-driven Configuration Dialog */}
      <Dialog open={Boolean(editing)} onOpenChange={(open) => { if (!open && !saving) setEditing(null); }}>
        <DialogContent className="flex max-h-[86dvh] flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
          {editing ? (
            <form
              className="flex min-h-0 min-w-0 flex-1 flex-col"
              onSubmit={(ev: FormEvent) => {
                ev.preventDefault();
                if (missingRequiredNode || saving) return;
                void handleSave(editing.canApplyInPlace && !editing.installed);
              }}
            >
              <DialogHeader className="shrink-0 border-b border-border/60 px-5 py-4 pr-12">
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

              <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-4 overflow-x-hidden overflow-y-auto px-5 py-4">
                {saveError ? (
                  <Alert variant="destructive">
                    <TriangleAlert className="size-4" />
                    <AlertTitle>{zh ? "操作失败" : "Failed"}</AlertTitle>
                    <AlertDescription className="wrap-anywhere">{saveError}</AlertDescription>
                  </Alert>
                ) : null}

                {/* Zone 1: Basic & Required */}
                <FieldGroup className="min-w-0 gap-3.5">
                  {editing.presets.length > 1 ? (() => {
                    const presetOptions = editing.presets.map((p) => ({
                      value: p.id,
                      label: `${loc(p.label, lang, p.id)} (${p.id})`,
                    }));
                    return (
                      <Field className="min-w-0">
                        <FieldLabel htmlFor="addon-preset">{zh ? "部署拓扑预设" : "Deployment preset"}</FieldLabel>
                        <Select
                          items={presetOptions}
                          value={preset}
                          onValueChange={(val) => {
                            if (val) handlePresetChange(val);
                          }}
                        >
                          <SelectTrigger id="addon-preset" className="w-full min-w-0">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent alignItemWithTrigger={false}>
                            <SelectGroup>
                              {presetOptions.map((opt) => (
                                <SelectItem key={opt.value} value={opt.value}>
                                  {opt.label}
                                </SelectItem>
                              ))}
                            </SelectGroup>
                          </SelectContent>
                        </Select>
                        <FieldDescription>
                          {loc(editing.presets.find((p) => p.id === preset)?.description, lang)}
                        </FieldDescription>
                      </Field>
                    );
                  })() : null}
                  {tieredFields.basic.map(renderField)}
                </FieldGroup>

                {/* Live Resource & Memory Precheck Banner */}
                {resourcePrecheck ? (
                  resourcePrecheck.level === "danger" ? (
                    <Alert variant="destructive">
                      <TriangleAlert className="size-4" />
                      <AlertTitle>
                        {zh ? "目标节点内存不足，部署可能失败或触发 OOM" : "Insufficient memory on target node"}
                      </AlertTitle>
                      <AlertDescription className="text-xs">
                        {resourcePrecheck.targetNode ? (
                          <p>
                            {zh
                              ? `${editing.name} 至少需要 ${fmtMemory(resourcePrecheck.reqs.minMemoryMb)} 内存（建议 ≥ ${fmtMemory(resourcePrecheck.reqs.recommendedMemoryMb)}）。当前目标节点 ${resourcePrecheck.targetNode.name} 剩余 ${fmtMemory(resourcePrecheck.targetNode.memoryAvailableMb)} / 总计 ${fmtMemory(resourcePrecheck.targetNode.memoryTotalMb)}。建议切换到内存更充裕的节点或扩容后再安装。`
                              : `${editing.name} needs at least ${fmtMemory(resourcePrecheck.reqs.minMemoryMb)} RAM (recommended >= ${fmtMemory(resourcePrecheck.reqs.recommendedMemoryMb)}). Target node ${resourcePrecheck.targetNode.name} currently has ${fmtMemory(resourcePrecheck.targetNode.memoryAvailableMb)} free / ${fmtMemory(resourcePrecheck.targetNode.memoryTotalMb)} total.`}
                          </p>
                        ) : null}
                        {loc(resourcePrecheck.reqs.note, lang) ? (
                          <p className="mt-1 opacity-90">{loc(resourcePrecheck.reqs.note, lang)}</p>
                        ) : null}
                      </AlertDescription>
                    </Alert>
                  ) : resourcePrecheck.level === "warn" ? (
                    <Alert className="border-amber-500/40 bg-amber-500/5 text-foreground [&>svg]:text-amber-600 dark:[&>svg]:text-amber-400">
                      <TriangleAlert className="size-4" />
                      <AlertTitle>
                        {zh ? "目标节点内存偏紧（低于建议配置）" : "Target node memory is below recommended"}
                      </AlertTitle>
                      <AlertDescription className="text-xs text-muted-foreground">
                        {resourcePrecheck.targetNode ? (
                          <p>
                            {zh
                              ? `${editing.name} 建议至少 ${fmtMemory(resourcePrecheck.reqs.recommendedMemoryMb)} 内存。当前节点 ${resourcePrecheck.targetNode.name} 剩余 ${fmtMemory(resourcePrecheck.targetNode.memoryAvailableMb)} / 总计 ${fmtMemory(resourcePrecheck.targetNode.memoryTotalMb)}，可继续部署但高负载下可能受限。`
                              : `${editing.name} recommends >= ${fmtMemory(resourcePrecheck.reqs.recommendedMemoryMb)} RAM. Target node ${resourcePrecheck.targetNode.name} has ${fmtMemory(resourcePrecheck.targetNode.memoryAvailableMb)} free / ${fmtMemory(resourcePrecheck.targetNode.memoryTotalMb)} total.`}
                          </p>
                        ) : null}
                        {loc(resourcePrecheck.reqs.note, lang) ? (
                          <p className="mt-1">{loc(resourcePrecheck.reqs.note, lang)}</p>
                        ) : null}
                      </AlertDescription>
                    </Alert>
                  ) : (
                    <div className="flex items-start gap-2.5 rounded-lg border border-border bg-muted/25 p-3 text-xs">
                      {resourcePrecheck.level === "ok" ? (
                        <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
                      ) : (
                        <Cpu className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                      )}
                      <div className="flex flex-col gap-0.5">
                        <div className="font-medium text-foreground">
                          {resourcePrecheck.targetNode
                            ? zh
                              ? `资源预检通过 · 目标节点 ${resourcePrecheck.targetNode.name}`
                              : `Resource precheck passed · Target node ${resourcePrecheck.targetNode.name}`
                            : zh
                              ? `建议资源：≥ ${fmtMemory(resourcePrecheck.reqs.recommendedMemoryMb)} 内存`
                              : `Recommended resources: >= ${fmtMemory(resourcePrecheck.reqs.recommendedMemoryMb)} RAM`}
                        </div>
                        <div className="text-muted-foreground">
                          {resourcePrecheck.targetNode && resourcePrecheck.targetNode.memoryTotalMb > 0
                            ? zh
                              ? `剩余 ${fmtMemory(resourcePrecheck.targetNode.memoryAvailableMb)} / 总内存 ${fmtMemory(resourcePrecheck.targetNode.memoryTotalMb)}（建议 ≥ ${fmtMemory(resourcePrecheck.reqs.recommendedMemoryMb)}${resourcePrecheck.sameNodeRedeploy ? "；当前插件已运行在该节点" : ""}）`
                              : `${fmtMemory(resourcePrecheck.targetNode.memoryAvailableMb)} free / ${fmtMemory(resourcePrecheck.targetNode.memoryTotalMb)} total (recommended >= ${fmtMemory(resourcePrecheck.reqs.recommendedMemoryMb)}${resourcePrecheck.sameNodeRedeploy ? "; addon already running on this node" : ""})`
                            : loc(resourcePrecheck.reqs.note, lang)}
                        </div>
                      </div>
                    </div>
                  )
                ) : null}

                {/* Zone 2: Recommended + Control Credential Inheritance (Builder) */}
                {(tieredFields.recommended.length > 0 || (editing.id === "builder" && (savedRegistries.length > 0 || savedGitProviders.length > 0))) ? (
                  <div className="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-muted/20 p-3.5">
                    <div className="text-xs font-medium text-foreground">
                      {zh ? "推荐配置（镜像仓库与凭据复用）" : "Recommended (Registry & credential reuse)"}
                    </div>

                    {editing.id === "builder" && savedRegistries.length > 0 ? (() => {
                      const registryOptions = [
                        {
                          value: "__manual__",
                          label: zh ? "手动填写或保留现有配置" : "Enter manually or keep current",
                        },
                        ...savedRegistries.map((r) => ({
                          value: r.host,
                          label: `${r.host} (${r.username})`,
                        })),
                      ];
                      return (
                        <Field className="min-w-0">
                          <FieldLabel htmlFor="inherit-registry">
                            {zh ? "一键复用 Control 已保存的镜像仓库凭据" : "Reuse saved Control registry credential"}
                          </FieldLabel>
                          <Select
                            items={registryOptions}
                            value={inheritRegistryHost || "__manual__"}
                            onValueChange={(val) => {
                              handleSelectInheritedRegistry(!val || val === "__manual__" ? "" : val);
                            }}
                          >
                            <SelectTrigger id="inherit-registry" className="w-full min-w-0">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent alignItemWithTrigger={false}>
                              <SelectGroup>
                                {registryOptions.map((opt) => (
                                  <SelectItem key={opt.value} value={opt.value}>
                                    <span className="truncate">{opt.label}</span>
                                  </SelectItem>
                                ))}
                              </SelectGroup>
                            </SelectContent>
                          </Select>
                          <FieldDescription>
                            {zh
                              ? "选择后将自动复用该仓库的地址、用户名和密码，无需重复输入密码。"
                              : "Automatically inherits the registry host, username, and password from Control."}
                          </FieldDescription>
                        </Field>
                      );
                    })() : null}

                    {editing.id === "builder" && savedGitProviders.length > 0 ? (
                      <div className="flex min-w-0 flex-col gap-1.5">
                        <span className="text-xs font-medium text-foreground">
                          {zh ? "复用 Control 已保存的 Git 凭据（可选）" : "Reuse saved Control Git credentials (optional)"}
                        </span>
                        <div className="flex flex-wrap gap-2">
                          {savedGitProviders.map((prov) => {
                            const checked = inheritGitIds.includes(prov.id);
                            return (
                              <label
                                key={prov.id}
                                className="flex cursor-pointer items-center gap-2 rounded-lg border border-input bg-background px-2.5 py-1.5 text-xs transition-colors hover:bg-accent/40 has-data-checked:border-primary/60 has-data-checked:bg-primary/5"
                              >
                                <Checkbox
                                  checked={checked}
                                  onCheckedChange={() => toggleInheritGit(prov.id)}
                                />
                                <span className="font-mono">{prov.type}:{prov.account}</span>
                              </label>
                            );
                          })}
                        </div>
                      </div>
                    ) : null}

                    <FieldGroup className="min-w-0 gap-3">
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
                        <Button type="button" variant="outline" size="sm" className="w-full justify-between" />
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

                {justDeployedAddonId === "builder" && (!builderStatus || onlineBuilderRunners.length === 0) ? (
                  <BuilderRunnerOnboardingCard
                    compact
                    lang={lang}
                    builderUrl={formConfig.domain ? `https://${formConfig.domain}` : editing.url}
                    rechecking={recheckingRunners}
                    onRecheck={() => void loadBuilderStatus()}
                  />
                ) : null}
              </div>

              <DialogFooter className="m-0 shrink-0 gap-2 rounded-b-xl border-t border-border/60 bg-muted/40 px-5 py-3.5">
                <Button type="button" variant="outline" disabled={Boolean(saving)} onClick={() => setEditing(null)}>
                  {zh ? "关闭" : "Close"}
                </Button>
                <Button
                  type="button"
                  variant={editing.canApplyInPlace ? "outline" : "default"}
                  disabled={Boolean(saving) || missingRequiredNode}
                  onClick={() => void handleSave(false)}
                >
                  {saving === "save" ? <Spinner className="mr-1.5 size-3.5" /> : null}
                  {zh ? "仅保存配置" : "Save config"}
                </Button>
                {editing.canApplyInPlace ? (
                  <Button type="button" disabled={Boolean(saving) || missingRequiredNode} onClick={() => void handleSave(true)}>
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

      {/* Static-bundle failure log modal */}
      <Dialog
        open={bundleLogOpen && Boolean(bundleOp)}
        onOpenChange={(open) => {
          setBundleLogOpen(open);
          if (!open && bundleOp?.status === "error") {
            setBundleOp(null);
          }
        }}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {zh ? `${bundleOp?.addonName || "Dashboard"} 更新失败日志` : `${bundleOp?.addonName || "Dashboard"} update failure logs`}
            </DialogTitle>
            <DialogDescription>
              {zh
                ? "本次热更新未切换当前生效版本，现有控制台未受影响。"
                : "The active bundle was not modified; your running console remains unaffected."}
            </DialogDescription>
          </DialogHeader>
          {bundleOp?.error ? (
            <Alert variant="destructive">
              <TriangleAlert className="size-4" />
              <AlertTitle>{zh ? "错误信息" : "Error"}</AlertTitle>
              <AlertDescription className="wrap-anywhere text-xs">{bundleOp.error}</AlertDescription>
            </Alert>
          ) : null}
          {bundleOp?.logs.length ? (
            <pre className="max-h-60 overflow-auto rounded-lg border border-border bg-muted/40 p-3 font-mono text-xs text-muted-foreground">
              {bundleOp.logs.join("\n")}
            </pre>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                setBundleLogOpen(false);
                setBundleOp(null);
              }}
            >
              {zh ? "关闭" : "Close"}
            </Button>
            {bundleOp && (bundleOp.action === "update" || bundleOp.action === "rollback") ? (
              <Button
                type="button"
                onClick={() => {
                  const target = addons.find((item) => item.id === bundleOp.addonId);
                  setBundleLogOpen(false);
                  if (!target) {
                    setBundleOp(null);
                    return;
                  }
                  if (bundleOp.action === "rollback") {
                    void handleBundleRollback(target);
                  } else {
                    void handleBundleHotSwap(target);
                  }
                }}
              >
                <RefreshCw className="mr-1 size-3.5" />
                {zh ? "重试" : "Retry"}
              </Button>
            ) : null}
          </DialogFooter>
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
          <label className="flex cursor-pointer items-center gap-2.5 text-xs text-muted-foreground">
            <Checkbox
              checked={purgeSecrets}
              onCheckedChange={(checked) => setPurgeSecrets(Boolean(checked))}
            />
            <span>{zh ? "同时清除该插件作用域下的 Secrets" : "Also delete scoped secrets for this addon"}</span>
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
