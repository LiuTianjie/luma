import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Input } from "@/components/ui/input";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, ArrowRight, GitBranch, HardDrive, KeyRound, PackageCheck, Plus, RefreshCw, ShieldCheck } from "lucide-react";
import {
  fetchControlResources,
  removeGitProvider,
  removeRegistry,
  removeSecret,
  setGitProvider,
  setRegistry,
  setSecret,
  type GitProviderCredential,
  type RegistryCredential,
} from "../controlResourcesApi";
import { CodeCell, PrimaryCell, StatePill } from "../components/primitives";
import { useConfirm } from "../components/ConfirmDialog";
import type { DashboardStorageClass, Lang } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { useRouter } from "../router";

type CredentialsState = {
  secrets: string[];
  registries: RegistryCredential[];
  gitProviders: GitProviderCredential[];
  storageClasses: DashboardStorageClass[];
  loading: boolean;
  error: string;
};

function parseSecretName(value: string) {
  if (!value.includes("/")) return { scope: "", name: value };
  const [scope, name] = value.split("/", 2);
  return { scope, name: name || scope };
}

function registryLabel(item: RegistryCredential) {
  return item.serverAddress || item.host || "-";
}

function registryUser(item: RegistryCredential) {
  return item.username ? item.username : "-";
}

function gitProviderLabel(item: GitProviderCredential) {
  return item.id || `${item.type || "git"}:${item.account || "-"}`;
}

function gitProviderTypeLabel(item: GitProviderCredential) {
  return item.type === "github" ? "GitHub" : "Git / Gitea";
}

function secretScopeLabel(scope: string, lang: Lang) {
  if (!scope) return lang === "zh" ? "全局" : "global";
  return scope;
}

function secretLabel(secret: ParsedSecret) {
  return secret.scope ? `${secret.scope}/${secret.name}` : secret.name;
}

type ParsedSecret = ReturnType<typeof parseSecretName>;

type SecretGroup = {
  id: string;
  label: string;
  description: string;
  secrets: ParsedSecret[];
};

const GLOBAL_SECRET_GROUPS = [
  { id: "platform", prefixes: ["LAE_", "LUMA_"], zh: "Luma / LAE", en: "Luma / LAE", zhDescription: "平台控制面与应用引擎", enDescription: "Platform control plane and application engine" },
  { id: "ai", prefixes: ["ARK_", "OPENAI_", "ANTHROPIC_", "DEEPSEEK_", "ITOOL_TECH_ARK_"], zh: "AI 模型", en: "AI models", zhDescription: "模型、推理服务与 Agent", enDescription: "Models, inference services, and agents" },
  { id: "source", prefixes: ["CODEX_GITEA_", "GITEA_", "GITHUB_", "GITLAB_"], zh: "代码与仓库", en: "Source control", zhDescription: "Git Provider、Webhook 与仓库访问", enDescription: "Git providers, webhooks, and repository access" },
  { id: "network", prefixes: ["CLOUDFLARE_", "TAILSCALE_", "TRAEFIK_"], zh: "网络与域名", en: "Network and DNS", zhDescription: "边缘网络、DNS 与流量入口", enDescription: "Edge network, DNS, and ingress" },
  { id: "granary", prefixes: ["GRANARY_"], zh: "Granary", en: "Granary", zhDescription: "Granary 服务与数据层", enDescription: "Granary services and data layer" },
  { id: "itool", prefixes: ["ITOOL_TECH_"], zh: "iTool.tech", en: "iTool.tech", zhDescription: "iTool.tech 产品与支付配置", enDescription: "iTool.tech product and billing config" },
  { id: "delivery", prefixes: ["SMTP_", "MAIL_", "EMAIL_", "WECHAT_", "ALIPAY_", "STRIPE_"], zh: "通知与支付", en: "Delivery and billing", zhDescription: "邮件、通知与支付渠道", enDescription: "Email, notifications, and payment channels" },
] as const;

function buildSecretGroups(secrets: ParsedSecret[], lang: Lang): SecretGroup[] {
  const zh = lang === "zh";
  const groups = new Map<string, SecretGroup>();

  for (const secret of secrets) {
    if (secret.scope) {
      const id = `scope:${secret.scope}`;
      const current = groups.get(id) || {
        id,
        label: secret.scope,
        description: zh ? "应用作用域" : "Application scope",
        secrets: [],
      };
      current.secrets.push(secret);
      groups.set(id, current);
      continue;
    }

    const definition = GLOBAL_SECRET_GROUPS.find((candidate) =>
      candidate.prefixes.some((prefix) => secret.name.startsWith(prefix)),
    );
    const id = definition ? `global:${definition.id}` : "global:other";
    const current = groups.get(id) || {
      id,
      label: definition ? (zh ? definition.zh : definition.en) : (zh ? "其他全局配置" : "Other global config"),
      description: definition
        ? (zh ? definition.zhDescription : definition.enDescription)
        : (zh ? "尚未归入产品命名空间的全局 Secret" : "Global secrets outside a product namespace"),
      secrets: [],
    };
    current.secrets.push(secret);
    groups.set(id, current);
  }

  return [...groups.values()]
    .map((group) => ({ ...group, secrets: [...group.secrets].sort((left, right) => left.name.localeCompare(right.name)) }))
    .sort((left, right) => {
      const leftScoped = left.id.startsWith("scope:");
      const rightScoped = right.id.startsWith("scope:");
      if (leftScoped !== rightScoped) return leftScoped ? -1 : 1;
      const leftIndex = GLOBAL_SECRET_GROUPS.findIndex((item) => `global:${item.id}` === left.id);
      const rightIndex = GLOBAL_SECRET_GROUPS.findIndex((item) => `global:${item.id}` === right.id);
      const normalizedLeft = leftIndex < 0 ? Number.MAX_SAFE_INTEGER : leftIndex;
      const normalizedRight = rightIndex < 0 ? Number.MAX_SAFE_INTEGER : rightIndex;
      return normalizedLeft - normalizedRight || left.label.localeCompare(right.label);
    });
}

export function CredentialsPage({
  lang,
  token,
  vm,
}: {
  lang: Lang;
  token: string;
  vm: DashboardViewModel;
}) {
  const zh = lang === "zh";
  const { path, search, navigate } = useRouter();
  const section = path.split("/")[2] || "secrets";
  const activeTab = ["registries", "git", "storage", "maintenance"].includes(section) ? section : "secrets";
  const sectionMeta = {
    secrets: { zh: "密钥", en: "Secrets", zhDescription: "管理应用和平台密钥。敏感值只写不回显。", enDescription: "Manage application and platform secrets. Sensitive values are write-only." },
    registries: { zh: "镜像仓库凭据", en: "Registry credentials", zhDescription: "管理私有镜像仓库的拉取凭据。", enDescription: "Manage credentials for pulling from private registries." },
    git: { zh: "Git 凭据", en: "Git credentials", zhDescription: "管理代码仓库访问凭据。", enDescription: "Manage credentials for source repositories." },
    storage: { zh: "存储配置", en: "Storage configuration", zhDescription: "查看集群可用的存储类和节点端点。", enDescription: "View storage classes and node endpoints available to the cluster." },
    maintenance: { zh: "维护", en: "Maintenance", zhDescription: "进入基础设施维护，执行升级和路由检查。", enDescription: "Open infrastructure maintenance for upgrades and route checks." },
  }[activeTab] || {
    zh: "设置", en: "Settings", zhDescription: "管理设置。", enDescription: "Manage settings.",
  };
  const editing = path.endsWith("/new") && ["secrets", "registries", "git"].includes(activeTab);
  const [state, setState] = useState<CredentialsState>({
    secrets: [],
    registries: [],
    gitProviders: [],
    storageClasses: vm.storageClasses,
    loading: true,
    error: "",
  });

  // Write-form state. Sensitive values live only in the form fields and are
  // cleared right after a successful submit; they are never persisted to the
  // read state and never rendered back.
  const [secretForm, setSecretForm] = useState({ name: "", scope: "", value: "" });
  const [registryForm, setRegistryForm] = useState({ host: "", username: "", password: "" });
  const [gitProviderForm, setGitProviderForm] = useState({ type: "github", account: "", baseUrl: "", cloneBaseUrl: "", username: "", token: "" });
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [writeError, setWriteError] = useState("");
  const { confirm, element: confirmDialog } = useConfirm(lang);
  const [expandedSecretGroups, setExpandedSecretGroups] = useState<Set<string>>(new Set());
  const initializedSecretGroups = useRef(false);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    setState((current) => ({ ...current, loading: true, error: "" }));
    try {
      const resources = await fetchControlResources({ token, signal });
      setState({
        secrets: resources.secrets || [],
        registries: resources.registries || [],
        gitProviders: resources.providers || [],
        storageClasses: resources.storageClasses || vm.storageClasses,
        loading: false,
        error: "",
      });
    } catch (error) {
      if (signal?.aborted) return;
      setState((current) => ({ ...current, loading: false, error: String(error instanceof Error ? error.message : error) }));
    }
  }, [token, vm.storageClasses]);

  useEffect(() => {
    if (activeTab === "maintenance") {
      setState(current => ({ ...current, loading: false }));
      return;
    }
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh, activeTab]);

  useEffect(() => {
    if (activeTab === "maintenance") return;
    const onRefresh = () => void refresh();
    window.addEventListener("luma:refresh", onRefresh);
    return () => window.removeEventListener("luma:refresh", onRefresh);
  }, [refresh, activeTab]);

  useEffect(() => {
    const params = new URLSearchParams(search);
    setSecretForm({ name: params.get("name") || "", scope: params.get("scope") || "", value: "" });
    setRegistryForm({ host: "", username: "", password: "" });
    setGitProviderForm({ type: "github", account: "", baseUrl: "", cloneBaseUrl: "", username: "", token: "" });
    setWriteError("");
  }, [path, search]);

  const parsedSecrets = useMemo(() => state.secrets.map(parseSecretName), [state.secrets]);
  const secretGroups = useMemo(() => buildSecretGroups(parsedSecrets, lang), [lang, parsedSecrets]);


  useEffect(() => {
    if (state.loading || initializedSecretGroups.current || !secretGroups.length) return;
    initializedSecretGroups.current = true;
    setExpandedSecretGroups(new Set([secretGroups[0].id]));
  }, [secretGroups, state.loading]);

  const submitSecret = async () => {
    if (!secretForm.name.trim() || !secretForm.value) return;
    setBusy("secret");
    setNotice("");
    setWriteError("");
    try {
      await setSecret({ token, name: secretForm.name.trim(), value: secretForm.value, scope: secretForm.scope.trim() });
      setSecretForm({ name: "", scope: "", value: "" });
      setNotice(zh ? `Secret 已保存：${secretForm.name.trim()}` : `Secret saved: ${secretForm.name.trim()}`);
      await refresh();
      navigate("/settings/secrets");
    } catch (error) {
      setWriteError(String(error instanceof Error ? error.message : error));
    } finally {
      setBusy("");
    }
  };

  const submitRegistry = async () => {
    if (!registryForm.host.trim() || !registryForm.username.trim() || !registryForm.password) return;
    setBusy("registry");
    setNotice("");
    setWriteError("");
    try {
      await setRegistry({ token, host: registryForm.host.trim(), username: registryForm.username.trim(), password: registryForm.password });
      setRegistryForm({ host: "", username: "", password: "" });
      setNotice(zh ? `Registry 凭据已保存：${registryForm.host.trim()}` : `Registry credential saved: ${registryForm.host.trim()}`);
      await refresh();
      navigate("/settings/registries");
    } catch (error) {
      setWriteError(String(error instanceof Error ? error.message : error));
    } finally {
      setBusy("");
    }
  };

  const deleteSecret = async (secret: ParsedSecret) => {
    const label = secretLabel(secret);
    const ok = await confirm({
      title: zh ? `删除 Secret ${label}？` : `Remove secret ${label}?`,
      body: zh
        ? <p>值是只写的，删除后无法恢复，需要重新录入。已经运行的实例不受影响，但后续部署将拿不到这个值。</p>
        : <p>Values are write-only, so this cannot be recovered — it would have to be entered again. Running instances are unaffected, but future deployments will no longer resolve this value.</p>,
      confirmLabel: zh ? "删除" : "Remove",
    });
    if (!ok) return;
    setBusy(`remove-secret-${label}`);
    setNotice("");
    setWriteError("");
    try {
      await removeSecret({ token, name: secret.name, scope: secret.scope || undefined });
      setNotice(zh ? `Secret 已删除：${label}` : `Secret removed: ${label}`);
      await refresh();
    } catch (error) {
      setWriteError(String(error instanceof Error ? error.message : error));
    } finally {
      setBusy("");
    }
  };

  const deleteRegistry = async (host: string) => {
    if (!host || host === "-") return;
    const ok = await confirm({
      title: zh ? `删除 ${host} 的 registry 凭据？` : `Remove registry credential for ${host}?`,
      body: zh
        ? <p>之后从该 registry 拉取私有镜像会失败，已经拉取到节点上的镜像不受影响。凭据需要重新录入。</p>
        : <p>Pulling private images from this registry will fail afterwards; images already on the nodes are unaffected. The credential would have to be entered again.</p>,
      confirmLabel: zh ? "删除" : "Remove",
    });
    if (!ok) return;
    setBusy(`remove-${host}`);
    setNotice("");
    setWriteError("");
    try {
      await removeRegistry({ token, host });
      setNotice(zh ? `Registry 凭据已删除：${host}` : `Registry credential removed: ${host}`);
      await refresh();
    } catch (error) {
      setWriteError(String(error instanceof Error ? error.message : error));
    } finally {
      setBusy("");
    }
  };

  const submitGitProvider = async () => {
    if (!gitProviderForm.account.trim() || !gitProviderForm.token) return;
    if (gitProviderForm.type === "gitea" && !gitProviderForm.baseUrl.trim()) return;
    setBusy("git-provider");
    setNotice("");
    setWriteError("");
    try {
      await setGitProvider({
        token,
        providerType: gitProviderForm.type,
        account: gitProviderForm.account.trim(),
        baseUrl: gitProviderForm.baseUrl.trim(),
        cloneBaseUrl: gitProviderForm.cloneBaseUrl.trim(),
        username: gitProviderForm.username.trim(),
        gitToken: gitProviderForm.token,
      });
      const savedId = `${gitProviderForm.type}:${gitProviderForm.account.trim()}`;
      setGitProviderForm({ type: gitProviderForm.type, account: "", baseUrl: gitProviderForm.type === "gitea" ? gitProviderForm.baseUrl : "", cloneBaseUrl: "", username: "", token: "" });
      setNotice(zh ? `Git 凭据已保存：${savedId}` : `Git credential saved: ${savedId}`);
      await refresh();
      navigate("/settings/git");
    } catch (error) {
      setWriteError(String(error instanceof Error ? error.message : error));
    } finally {
      setBusy("");
    }
  };

  const deleteGitProvider = async (id: string) => {
    if (!id || id === "-") return;
    const ok = await confirm({
      title: zh ? `删除 ${id} 的 Git 凭据？` : `Remove Git credential for ${id}?`,
      body: zh
        ? <p>使用该账户的仓库导入和“从 Git 更新”会失败，直到重新录入凭据。已部署的应用继续运行。</p>
        : <p>Repository imports and "update from Git" for this account will fail until the credential is re-entered. Already-deployed applications keep running.</p>,
      confirmLabel: zh ? "删除" : "Remove",
    });
    if (!ok) return;
    setBusy(`remove-git-${id}`);
    setNotice("");
    setWriteError("");
    try {
      await removeGitProvider({ token, id });
      setNotice(zh ? `Git 凭据已删除：${id}` : `Git credential removed: ${id}`);
      await refresh();
    } catch (error) {
      setWriteError(String(error instanceof Error ? error.message : error));
    } finally {
      setBusy("");
    }
  };

  const canWrite = ["secrets", "registries", "git"].includes(activeTab);
  const itemCount = activeTab === "secrets" ? state.secrets.length
    : activeTab === "registries" ? state.registries.length
      : activeTab === "git" ? state.gitProviders.length : state.storageClasses.length;
  const initialLoading = state.loading && !itemCount && activeTab !== "maintenance";
  const submitting = busy === "secret" || busy === "registry" || busy === "git-provider";
  const saveDisabled = Boolean(busy) || (activeTab === "registries"
    ? !registryForm.host.trim() || !registryForm.username.trim() || !registryForm.password
    : activeTab === "git"
      ? !gitProviderForm.account.trim() || !gitProviderForm.token || (gitProviderForm.type === "gitea" && !gitProviderForm.baseUrl.trim())
      : !secretForm.name.trim() || !secretForm.value);
  const formTitle = activeTab === "registries" ? (zh ? "保存拉取凭据" : "Save pull credentials")
    : activeTab === "git" ? (zh ? "保存仓库访问凭据" : "Save repository credentials")
      : (zh ? "新增或轮换密钥" : "Add or rotate a secret");
  const SectionIcon = activeTab === "registries" ? PackageCheck : activeTab === "git" ? GitBranch : activeTab === "storage" ? HardDrive : KeyRound;
  const providerOptions = [{ value: "github", label: "GitHub" }, { value: "gitea", label: "Git / Gitea" }];

  return (
    <div className="settings-workspace flex min-w-0 flex-col gap-6">
      <PageHeader meta={{
        metrics: [],
        eyebrow: zh ? "设置" : "Settings",
        title: editing ? (zh ? "新增或轮换凭据" : "Add or rotate credential") : (zh ? sectionMeta.zh : sectionMeta.en),
        description: editing ? (zh ? "敏感值只写不回显，保存后不会返回浏览器。" : "Sensitive values are write-only and never returned after saving.") : (zh ? sectionMeta.zhDescription : sectionMeta.enDescription),
        action: editing ? (
          <Button variant="outline" size="sm" disabled={Boolean(busy)} onClick={() => navigate(`/settings/${activeTab}`)}>{zh ? "返回列表" : "Back to list"}</Button>
        ) : canWrite ? (
          <Button size="sm" onClick={() => navigate(`/settings/${activeTab}/new`)}><Plus data-icon="inline-start" />{zh ? "新增 / 轮换凭据" : "Add / rotate credential"}</Button>
        ) : undefined,
      }} />

      {state.error && activeTab !== "maintenance" ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>{zh ? "读取失败" : "Load failed"}</AlertTitle>
          <AlertDescription>{state.error}</AlertDescription>
        </Alert>
      ) : null}
      {notice ? (
        <Alert>
          <ShieldCheck />
          <AlertTitle>{zh ? "操作完成" : "Completed"}</AlertTitle>
          <AlertDescription>{notice}</AlertDescription>
        </Alert>
      ) : null}
      {writeError ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>{zh ? "操作失败" : "Operation failed"}</AlertTitle>
          <AlertDescription>{writeError}</AlertDescription>
        </Alert>
      ) : null}

      {editing ? (
        <form className="w-full max-w-3xl" aria-labelledby="credential-form-title" onSubmit={(event) => {
          event.preventDefault();
          if (saveDisabled) return;
          if (activeTab === "registries") void submitRegistry();
          else if (activeTab === "git") void submitGitProvider();
          else void submitSecret();
        }}>
          <Card>
            <CardHeader>
              <CardTitle id="credential-form-title">{formTitle}</CardTitle>
              <CardDescription>{activeTab === "registries"
                ? (zh ? "用于拉取私有镜像。密码和 Token 保存后不回显。" : "Used to pull private images. Saved passwords and tokens are never displayed.")
                : activeTab === "git"
                  ? (zh ? "同一服务可保存多个账户，导入仓库时选择对应账户。" : "Save multiple accounts per provider and choose an account when importing a repository.")
                  : (zh ? "同名保存即轮换。删除或轮换只影响后续部署，正在运行的实例不受影响。" : "Saving an existing name rotates its value. Changes affect future deployments; running instances are unaffected.")}</CardDescription>
            </CardHeader>
            <CardContent>
              <FieldGroup>
                {activeTab === "registries" ? (
                  <>
                    <Field data-disabled={Boolean(busy)}>
                      <FieldLabel htmlFor="registry-host">{zh ? "Registry 主机" : "Registry host"}</FieldLabel>
                      <Input id="registry-host" name="host" required disabled={Boolean(busy)} value={registryForm.host} placeholder="ghcr.io" autoComplete="off" onChange={(event) => setRegistryForm((current) => ({ ...current, host: event.target.value }))} />
                    </Field>
                    <Field data-disabled={Boolean(busy)}>
                      <FieldLabel htmlFor="registry-username">{zh ? "用户名" : "Username"}</FieldLabel>
                      <Input id="registry-username" name="username" required disabled={Boolean(busy)} autoComplete="off" value={registryForm.username} onChange={(event) => setRegistryForm((current) => ({ ...current, username: event.target.value }))} />
                    </Field>
                    <Field data-disabled={Boolean(busy)}>
                      <FieldLabel htmlFor="registry-password">{zh ? "密码 / Token" : "Password / token"}</FieldLabel>
                      <Input id="registry-password" name="password" type="password" required disabled={Boolean(busy)} autoComplete="new-password" value={registryForm.password} onChange={(event) => setRegistryForm((current) => ({ ...current, password: event.target.value }))} />
                    </Field>
                  </>
                ) : activeTab === "git" ? (
                  <>
                    <Field data-disabled={Boolean(busy)}>
                      <FieldLabel htmlFor="git-provider">{zh ? "代码托管服务" : "Provider"}</FieldLabel>
                      <Select items={providerOptions} value={gitProviderForm.type} disabled={Boolean(busy)} onValueChange={(value) => {
                        if (value) setGitProviderForm((current) => ({ ...current, type: value }));
                      }}>
                        <SelectTrigger id="git-provider" className="w-full"><SelectValue /></SelectTrigger>
                        <SelectContent alignItemWithTrigger={false}>
                          <SelectGroup>{providerOptions.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectGroup>
                        </SelectContent>
                      </Select>
                    </Field>
                    <Field data-disabled={Boolean(busy)}>
                      <FieldLabel htmlFor="git-account">{zh ? "账户名称" : "Account name"}</FieldLabel>
                      <Input id="git-account" name="account" required disabled={Boolean(busy)} value={gitProviderForm.account} placeholder={gitProviderForm.type === "github" ? "personal" : "work"} autoComplete="off" onChange={(event) => setGitProviderForm((current) => ({ ...current, account: event.target.value }))} />
                    </Field>
                    <Field data-disabled={Boolean(busy)}>
                      <FieldLabel htmlFor="git-username">{zh ? "用户名（可选）" : "Username (optional)"}</FieldLabel>
                      <Input id="git-username" name="username" disabled={Boolean(busy)} value={gitProviderForm.username} autoComplete="off" onChange={(event) => setGitProviderForm((current) => ({ ...current, username: event.target.value }))} />
                    </Field>
                    {gitProviderForm.type === "gitea" ? (
                      <>
                        <Field data-disabled={Boolean(busy)}>
                          <FieldLabel htmlFor="git-base-url">Base URL</FieldLabel>
                          <Input id="git-base-url" name="baseUrl" required disabled={Boolean(busy)} value={gitProviderForm.baseUrl} placeholder="https://gcode.example.com" onChange={(event) => setGitProviderForm((current) => ({ ...current, baseUrl: event.target.value }))} />
                        </Field>
                        <Field data-disabled={Boolean(busy)}>
                          <FieldLabel htmlFor="git-clone-url">{zh ? "Clone base URL（可选）" : "Clone base URL (optional)"}</FieldLabel>
                          <Input id="git-clone-url" name="cloneBaseUrl" disabled={Boolean(busy)} aria-describedby="git-clone-url-description" value={gitProviderForm.cloneBaseUrl} onChange={(event) => setGitProviderForm((current) => ({ ...current, cloneBaseUrl: event.target.value }))} />
                          <FieldDescription id="git-clone-url-description">{zh ? "留空时使用 Base URL。" : "Leave blank to use the Base URL."}</FieldDescription>
                        </Field>
                      </>
                    ) : null}
                    <Field data-disabled={Boolean(busy)}>
                      <FieldLabel htmlFor="git-token">Token / PAT</FieldLabel>
                      <Input id="git-token" name="token" type="password" required disabled={Boolean(busy)} autoComplete="new-password" value={gitProviderForm.token} onChange={(event) => setGitProviderForm((current) => ({ ...current, token: event.target.value }))} />
                    </Field>
                  </>
                ) : (
                  <>
                    <Field data-disabled={Boolean(busy)}>
                      <FieldLabel htmlFor="secret-name">{zh ? "名称" : "Name"}</FieldLabel>
                      <Input id="secret-name" name="name" required disabled={Boolean(busy)} value={secretForm.name} placeholder="DATABASE_URL" autoComplete="off" onChange={(event) => setSecretForm((current) => ({ ...current, name: event.target.value }))} />
                    </Field>
                    <Field data-disabled={Boolean(busy)}>
                      <FieldLabel htmlFor="secret-scope">{zh ? "作用域（可选）" : "Scope (optional)"}</FieldLabel>
                      <Input id="secret-scope" name="scope" disabled={Boolean(busy)} aria-describedby="secret-scope-description" value={secretForm.scope} autoComplete="off" onChange={(event) => setSecretForm((current) => ({ ...current, scope: event.target.value }))} />
                      <FieldDescription id="secret-scope-description">{zh ? "留空为全局密钥；填写应用名可限制作用域。" : "Leave blank for a global secret, or enter an application name to limit its scope."}</FieldDescription>
                    </Field>
                    <Field data-disabled={Boolean(busy)}>
                      <FieldLabel htmlFor="secret-value">{zh ? "值" : "Value"}</FieldLabel>
                      <Input id="secret-value" name="value" type="password" required disabled={Boolean(busy)} autoComplete="new-password" value={secretForm.value} onChange={(event) => setSecretForm((current) => ({ ...current, value: event.target.value }))} />
                    </Field>
                  </>
                )}
              </FieldGroup>
            </CardContent>
            <CardFooter className="flex-wrap justify-end gap-2">
              <Button variant="outline" type="button" disabled={Boolean(busy)} onClick={() => navigate(`/settings/${activeTab}`)}>{zh ? "取消" : "Cancel"}</Button>
              <Button type="submit" disabled={saveDisabled}>
                {submitting ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <ShieldCheck data-icon="inline-start" />}
                {submitting ? (zh ? "保存中…" : "Saving…") : (zh ? "保存" : "Save")}
              </Button>
            </CardFooter>
          </Card>
        </form>
      ) : activeTab === "maintenance" ? (
        <Card>
          <CardHeader>
            <CardTitle>{zh ? "系统维护" : "System maintenance"}</CardTitle>
            <CardDescription>{zh ? "管理控制面与节点升级，检查路由并查看任务结果。" : "Manage control-plane and node upgrades, check routes, and review task results."}</CardDescription>
          </CardHeader>
          <CardFooter>
            <Button type="button" size="sm" onClick={() => navigate("/fleet/maintenance")}>{zh ? "打开系统维护" : "Open system maintenance"}<ArrowRight data-icon="inline-end" /></Button>
          </CardFooter>
        </Card>
      ) : (
        <Card>
          <CardHeader className="has-data-[slot=card-action]:grid-cols-1 sm:has-data-[slot=card-action]:grid-cols-[1fr_auto]">
            <CardTitle>{zh ? "已保存的配置" : "Saved configuration"}</CardTitle>
            <CardDescription>{state.loading ? (zh ? "正在刷新配置…" : "Refreshing configuration…") : state.error && !itemCount ? (zh ? "读取配置后将在这里显示。" : "Configurations will appear here once loaded.") : activeTab === "secrets"
              ? (zh ? `${parsedSecrets.length} 个密钥，${secretGroups.length} 个分组` : `${parsedSecrets.length} secrets in ${secretGroups.length} groups`)
              : (zh ? `${itemCount} 条配置` : `${itemCount} configurations`)}</CardDescription>
            {activeTab === "secrets" && secretGroups.length ? (
              <CardAction className="col-span-full col-start-1 row-start-3 justify-self-start sm:col-span-1 sm:col-start-2 sm:row-span-2 sm:row-start-1 sm:justify-self-end">
                <div className="flex flex-wrap gap-2">
                  <Button variant="outline" size="sm" type="button" onClick={() => setExpandedSecretGroups(new Set(secretGroups.map((group) => group.id)))}>{zh ? "全部展开" : "Expand all"}</Button>
                  <Button variant="outline" size="sm" type="button" onClick={() => setExpandedSecretGroups(new Set())}>{zh ? "全部收起" : "Collapse all"}</Button>
                </div>
              </CardAction>
            ) : null}
          </CardHeader>
          <CardContent aria-busy={state.loading}>
            {initialLoading ? (
              <div className="flex flex-col gap-4" role="status" aria-label={zh ? "加载配置…" : "Loading configuration…"}>
                <Skeleton className="h-5 w-40" />
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-3/4" />
                <span className="sr-only">{zh ? "加载配置…" : "Loading configuration…"}</span>
              </div>
            ) : !itemCount ? (
              <Empty>
                <EmptyHeader>
                  <EmptyMedia variant="icon"><SectionIcon /></EmptyMedia>
                  <EmptyTitle>{state.error ? (zh ? "暂时无法加载配置" : "Configuration unavailable") : (zh ? "暂无配置" : "No configuration yet")}</EmptyTitle>
                  <EmptyDescription>{state.error
                    ? (zh ? "请重试以获取最新配置。" : "Try again to retrieve the latest configuration.")
                    : canWrite ? (zh ? "添加凭据后，可在这里查看或管理。" : "Add a credential to view and manage it here.")
                      : (zh ? "配置存储类后，可在这里查看节点和端点。" : "Configured storage classes will show their nodes and endpoints here.")}</EmptyDescription>
                </EmptyHeader>
                {state.error || canWrite ? (
                  <EmptyContent>
                    <Button variant={state.error ? "outline" : "default"} type="button" onClick={() => state.error ? void refresh() : navigate(`/settings/${activeTab}/new`)}>
                      {state.error ? <RefreshCw data-icon="inline-start" /> : <Plus data-icon="inline-start" />}
                      {state.error ? (zh ? "重试" : "Retry") : (zh ? "添加凭据" : "Add credential")}
                    </Button>
                  </EmptyContent>
                ) : null}
              </Empty>
            ) : activeTab === "secrets" ? (
              <Accordion multiple value={[...expandedSecretGroups]} onValueChange={(values) => setExpandedSecretGroups(new Set(values))}>
                {secretGroups.map((group) => (
                  <AccordionItem key={group.id} value={group.id}>
                    <AccordionTrigger>
                      <span className="flex min-w-0 flex-1 items-center gap-2"><span className="truncate">{group.label}</span><Badge variant="secondary">{group.secrets.length}</Badge></span>
                    </AccordionTrigger>
                    <AccordionContent>
                      <div className="flex min-w-0 flex-col gap-4">
                        <p className="text-muted-foreground">{group.description}</p>
                        <Table aria-label={zh ? `${group.label}密钥` : `${group.label} secrets`} containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "密钥列表，可横向滚动" : "Secrets, horizontally scrollable" }}>
                          <TableHeader><TableRow>
                            <TableHead>{zh ? "名称" : "Name"}</TableHead>
                            <TableHead>{zh ? "作用域" : "Scope"}</TableHead>
                            <TableHead>{zh ? "值" : "Value"}</TableHead>
                            <TableHead>{zh ? "状态" : "Status"}</TableHead>
                            <TableHead className="text-right">{zh ? "操作" : "Actions"}</TableHead>
                          </TableRow></TableHeader>
                          <TableBody>{group.secrets.map((secret) => (
                            <TableRow key={`${secret.scope}/${secret.name}`}>
                              <TableCell><PrimaryCell title={secret.name} /></TableCell>
                              <TableCell><Badge variant="secondary">{secretScopeLabel(secret.scope, lang)}</Badge></TableCell>
                              <TableCell><Badge variant="outline">{zh ? "只写" : "Write-only"}</Badge></TableCell>
                              <TableCell><StatePill label={zh ? "已保存" : "Saved"} value="ready" /></TableCell>
                              <TableCell>
                                <div className="flex justify-end gap-2">
                                  <Button variant="outline" size="sm" type="button" disabled={Boolean(busy)} aria-label={zh ? `轮换 ${secretLabel(secret)}` : `Rotate ${secretLabel(secret)}`} onClick={() => navigate(`/settings/secrets/new?${new URLSearchParams({ name: secret.name, scope: secret.scope })}`)}>{zh ? "轮换" : "Rotate"}</Button>
                                  <Button variant="destructive" size="sm" type="button" disabled={Boolean(busy)} aria-label={zh ? `删除 ${secretLabel(secret)}` : `Remove ${secretLabel(secret)}`} onClick={() => void deleteSecret(secret)}>
                                    {busy === `remove-secret-${secretLabel(secret)}` ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}
                                    {busy === `remove-secret-${secretLabel(secret)}` ? (zh ? "删除中…" : "Removing…") : (zh ? "删除" : "Remove")}
                                  </Button>
                                </div>
                              </TableCell>
                            </TableRow>
                          ))}</TableBody>
                        </Table>
                      </div>
                    </AccordionContent>
                  </AccordionItem>
                ))}
              </Accordion>
            ) : activeTab === "registries" ? (
              <Table aria-label={zh ? "镜像仓库凭据" : "Registry credentials"} containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "镜像仓库凭据，可横向滚动" : "Registry credentials, horizontally scrollable" }}>
                <TableHeader><TableRow>
                  <TableHead>Registry</TableHead><TableHead>{zh ? "用户名" : "Username"}</TableHead><TableHead>{zh ? "状态" : "Status"}</TableHead><TableHead className="text-right">{zh ? "操作" : "Actions"}</TableHead>
                </TableRow></TableHeader>
                <TableBody>{state.registries.map((item) => (
                  <TableRow key={registryLabel(item)}>
                    <TableCell><PrimaryCell title={registryLabel(item)} meta={item.host} /></TableCell>
                    <TableCell><CodeCell value={registryUser(item)} /></TableCell>
                    <TableCell><StatePill label={item.configured ? (zh ? "已配置" : "Configured") : (zh ? "缺失" : "Missing")} value={item.configured ? "ready" : "missing"} /></TableCell>
                    <TableCell className="text-right"><Button variant="destructive" size="sm" type="button" disabled={Boolean(busy)} aria-label={zh ? `删除 ${registryLabel(item)} 的凭据` : `Remove credential for ${registryLabel(item)}`} onClick={() => void deleteRegistry(registryLabel(item))}>
                      {busy === `remove-${registryLabel(item)}` ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}
                      {busy === `remove-${registryLabel(item)}` ? (zh ? "删除中…" : "Removing…") : (zh ? "删除" : "Remove")}
                    </Button></TableCell>
                  </TableRow>
                ))}</TableBody>
              </Table>
            ) : activeTab === "git" ? (
              <Table aria-label={zh ? "Git 凭据" : "Git credentials"} containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "Git 凭据，可横向滚动" : "Git credentials, horizontally scrollable" }}>
                <TableHeader><TableRow>
                  <TableHead>{zh ? "服务" : "Provider"}</TableHead><TableHead>{zh ? "账户" : "Account"}</TableHead><TableHead>{zh ? "地址" : "Host"}</TableHead><TableHead>{zh ? "状态" : "Status"}</TableHead><TableHead className="text-right">{zh ? "操作" : "Actions"}</TableHead>
                </TableRow></TableHeader>
                <TableBody>{state.gitProviders.map((item) => (
                  <TableRow key={gitProviderLabel(item)}>
                    <TableCell><Badge variant="secondary">{gitProviderTypeLabel(item)}</Badge></TableCell>
                    <TableCell><PrimaryCell title={item.account || "-"} meta={item.username || item.id} /></TableCell>
                    <TableCell><CodeCell value={item.cloneBaseUrl || item.baseUrl || "-"} /></TableCell>
                    <TableCell><StatePill label={item.configured ? (zh ? "已配置" : "Configured") : (zh ? "缺失" : "Missing")} value={item.configured ? "ready" : "missing"} /></TableCell>
                    <TableCell className="text-right"><Button variant="destructive" size="sm" type="button" disabled={Boolean(busy)} aria-label={zh ? `删除 ${gitProviderLabel(item)} 的凭据` : `Remove credential for ${gitProviderLabel(item)}`} onClick={() => void deleteGitProvider(gitProviderLabel(item))}>
                      {busy === `remove-git-${gitProviderLabel(item)}` ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}
                      {busy === `remove-git-${gitProviderLabel(item)}` ? (zh ? "删除中…" : "Removing…") : (zh ? "删除" : "Remove")}
                    </Button></TableCell>
                  </TableRow>
                ))}</TableBody>
              </Table>
            ) : (
              <Table aria-label={zh ? "存储配置" : "Storage configuration"} containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "存储配置，可横向滚动" : "Storage configuration, horizontally scrollable" }}>
                <TableHeader><TableRow>
                  <TableHead>{zh ? "存储类" : "Storage class"}</TableHead><TableHead>{zh ? "提供方" : "Provider"}</TableHead><TableHead>{zh ? "模式" : "Mode"}</TableHead><TableHead>{zh ? "节点 / 端点" : "Node / endpoint"}</TableHead><TableHead>{zh ? "区域" : "Regions"}</TableHead>
                </TableRow></TableHeader>
                <TableBody>{state.storageClasses.map((item) => (
                  <TableRow key={item.name || "storage-class"}>
                    <TableCell><PrimaryCell title={item.name || "-"} /></TableCell>
                    <TableCell><Badge variant="secondary">{item.provider || "-"}</Badge></TableCell>
                    <TableCell><Badge variant="outline">{item.mode || "-"}</Badge></TableCell>
                    <TableCell><CodeCell value={item.node || item.endpoint || item.path || "-"} /></TableCell>
                    <TableCell><div className="flex flex-wrap gap-1">{item.regions?.length ? item.regions.map((region) => <Badge variant="secondary" key={region}>{region}</Badge>) : "-"}</div></TableCell>
                  </TableRow>
                ))}</TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      )}
      {confirmDialog}
    </div>
  );
}
