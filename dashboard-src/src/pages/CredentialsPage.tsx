import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, GitBranch, KeyRound, LockKeyhole, PackageCheck, ShieldCheck } from "lucide-react";
import {
  fetchGitProviders,
  fetchRegistries,
  fetchSecrets,
  fetchStorageClasses,
  removeGitProvider,
  removeRegistry,
  removeSecret,
  setGitProvider,
  setRegistry,
  setSecret,
  type GitProviderCredential,
  type RegistryCredential,
} from "../controlResourcesApi";
import { Badge, CodeCell, PrimaryCell, SelectControl, StatePill } from "../components/ui";
import { useConfirm } from "../components/ConfirmDialog";
import type { DashboardStorageClass, Lang } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";
import { useRouter, toHref } from "../router";

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
  const editing = path.endsWith("/new") && ["secrets", "registries", "git"].includes(activeTab);
  const setActiveTab = (tab: string) => navigate(`/settings/${tab}`);
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
      const [secrets, registries, gitProviders, storage] = await Promise.all([
        fetchSecrets({ token, signal }),
        fetchRegistries({ token, signal }),
        fetchGitProviders({ token, signal }),
        fetchStorageClasses({ token, signal }),
      ]);
      setState({
        secrets: secrets.secrets || [],
        registries: registries.registries || [],
        gitProviders: gitProviders.providers || [],
        storageClasses: storage.storageClasses || vm.storageClasses,
        loading: false,
        error: "",
      });
    } catch (error) {
      if (signal?.aborted) return;
      setState((current) => ({ ...current, loading: false, error: String(error instanceof Error ? error.message : error) }));
    }
  }, [token, vm.storageClasses]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    const onRefresh = () => void refresh();
    window.addEventListener("luma:refresh", onRefresh);
    return () => window.removeEventListener("luma:refresh", onRefresh);
  }, [refresh]);

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

  const toggleSecretGroup = (id: string) => {
    setExpandedSecretGroups((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

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

  return (
    <>
      <PageHeader
        meta={{
          metrics: [],
          eyebrow: zh ? "设置" : "Settings",
          title: editing ? (zh ? "新增或轮换凭据" : "Add or rotate credential") : (zh ? "凭据与维护" : "Credentials and maintenance"),
          description: zh ? "管理访问凭据。敏感值只写不回显，保存后不会返回浏览器。" : "Manage access credentials. Sensitive values are write-only and never returned after saving.",
          action: editing ? <button type="button" className="ghost" disabled={!!busy} onClick={() => navigate(`/settings/${activeTab}`)}>{zh ? "返回列表" : "Back to list"}</button> : ["secrets", "registries", "git"].includes(activeTab) ? <button type="button" className="primary" onClick={() => navigate(`/settings/${activeTab}/new`)}>{zh ? "新增 / 轮换凭据" : "Add / rotate credential"}</button> : undefined,
        }}
      />

      {state.error ? (
        <div className="alert alert-error">
          <span>{state.error}</span>
        </div>
      ) : null}
      {notice ? (
        <div className="alert alert-success">
          <span>{notice}</span>
        </div>
      ) : null}
      {writeError ? (
        <div className="alert alert-error">
          <span>{writeError}</span>
        </div>
      ) : null}

      {state.loading && !state.secrets.length && !state.registries.length ? (
        <div className="panel page-loading-inline" aria-busy="true">
          <span className="skeleton skeleton-line skeleton-panel-title" />
          <span className="skeleton skeleton-line" />
          <span className="skeleton skeleton-line skeleton-medium" />
          <span className="skeleton skeleton-line skeleton-wide" />
          <p className="page-loading-label">{zh ? "加载凭据…" : "Loading credentials…"}</p>
        </div>
      ) : null}

      <section className="credentials-layout" style={{ display: "block" }} hidden={state.loading && !state.secrets.length && !state.registries.length}>
        <article className="panel credentials-index-panel" hidden={editing}>
          <div className="credentials-tabs" role="navigation" aria-label={zh ? "凭据视图" : "Credential views"}>
            <button type="button" className={activeTab === "secrets" ? "active" : ""} onClick={() => setActiveTab("secrets")}>
              <LockKeyhole size={15} aria-hidden="true" />
              Secrets
            </button>
            <button type="button" className={activeTab === "registries" ? "active" : ""} onClick={() => setActiveTab("registries")}>
              <PackageCheck size={15} aria-hidden="true" />
              Registries
            </button>
            <button type="button" className={activeTab === "git" ? "active" : ""} onClick={() => setActiveTab("git")}>
              <GitBranch size={15} aria-hidden="true" />
              Git Providers
            </button>
            <button type="button" className={activeTab === "storage" ? "active" : ""} onClick={() => setActiveTab("storage")}>
              {zh ? "存储配置" : "Storage configuration"}
            </button>
            <button type="button" className={activeTab === "maintenance" ? "active" : ""} onClick={() => setActiveTab("maintenance")}>
              {zh ? "系统维护" : "Maintenance"}
            </button>
          </div>

          {activeTab === "maintenance" ? <div className="empty-state"><h2>{zh ? "集群维护" : "Cluster maintenance"}</h2><p>{zh ? "查看系统版本、升级 CLI 与 Agent，并跟踪升级任务。" : "Inspect system versions, upgrade CLI and agents, and track maintenance tasks."}</p><a className="primary" href={toHref("/fleet/maintenance")} onClick={(event) => { if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); navigate("/fleet/maintenance"); }}>{zh ? "进入系统维护" : "Open maintenance"}</a></div> : null}
          {activeTab === "secrets" ? (
            parsedSecrets.length ? (
              <div className="secret-groups">
                <div className="secret-groups-toolbar">
                  <span>{zh ? `${secretGroups.length} 个分组` : `${secretGroups.length} groups`}</span>
                  <div>
                    <button type="button" className="ghost" onClick={() => setExpandedSecretGroups(new Set(secretGroups.map((group) => group.id)))}>
                      {zh ? "全部展开" : "Expand all"}
                    </button>
                    <button type="button" className="ghost" onClick={() => setExpandedSecretGroups(new Set())}>
                      {zh ? "全部收起" : "Collapse all"}
                    </button>
                  </div>
                </div>
                {secretGroups.map((group) => {
                  const expanded = expandedSecretGroups.has(group.id);
                  const contentId = `secret-group-${group.id.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
                  return (
                    <section className={expanded ? "secret-group expanded" : "secret-group"} key={group.id}>
                      <button
                        type="button"
                        className="secret-group-trigger"
                        aria-expanded={expanded}
                        aria-controls={contentId}
                        onClick={() => toggleSecretGroup(group.id)}
                      >
                        <span className="secret-group-mark" aria-hidden="true">{group.label.slice(0, 1).toUpperCase()}</span>
                        <span className="secret-group-copy">
                          <strong>{group.label}</strong>
                          <small>{group.description}</small>
                        </span>
                        <span className="secret-group-count">{group.secrets.length}</span>
                        <ChevronDown className="secret-group-chevron" size={16} aria-hidden="true" />
                      </button>
                      {expanded ? (
                        <div className="table-wrap secret-group-table" id={contentId}>
                          <table className="credentials-table">
                            <thead>
                              <tr>
                                <th>{zh ? "名称" : "Name"}</th>
                                <th>{zh ? "作用域" : "Scope"}</th>
                                <th>{zh ? "值" : "Value"}</th>
                                <th>{zh ? "状态" : "Status"}</th>
                                <th>{zh ? "操作" : "Actions"}</th>
                              </tr>
                            </thead>
                            <tbody>
                              {group.secrets.map((secret) => (
                                <tr key={`${secret.scope}/${secret.name}`}>
                                  <td><PrimaryCell title={secret.name} /></td>
                                  <td><Badge value={secretScopeLabel(secret.scope, lang)} /></td>
                                  <td><CodeCell value="write-only" /></td>
                                  <td><StatePill label={zh ? "已保存" : "saved"} value="ready" /></td>
                                  <td>
                                    <button type="button" className="ghost" disabled={!!busy} onClick={() => navigate(`/settings/secrets/new?${new URLSearchParams({ name: secret.name, scope: secret.scope })}`)}>{zh ? "轮换" : "Rotate"}</button>
                                    <button
                                      className="ghost danger"
                                      type="button"
                                      disabled={busy !== ""}
                                      onClick={() => void deleteSecret(secret)}
                                    >
                                      {busy === `remove-secret-${secretLabel(secret)}`
                                        ? (zh ? "删除中..." : "Removing...")
                                        : (zh ? "删除" : "Remove")}
                                    </button>
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      ) : null}
                    </section>
                  );
                })}
              </div>
            ) : (
              <div className="secret-groups-empty">{state.loading ? (zh ? "读取中..." : "Loading...") : (zh ? "暂无 Secret" : "No secrets")}</div>
            )
          ) : null}

          {activeTab === "registries" ? (
            <div className="table-wrap">
              <table className="credentials-table">
                <thead>
                  <tr>
                    <th>Registry</th>
                    <th>{zh ? "用户名" : "Username"}</th>
                    <th>{zh ? "状态" : "Status"}</th>
                    <th>{zh ? "操作" : "Actions"}</th>
                  </tr>
                </thead>
                <tbody>
                  {state.registries.length ? state.registries.map((item) => (
                    <tr key={registryLabel(item)}>
                      <td><PrimaryCell title={registryLabel(item)} meta={item.host} /></td>
                      <td><CodeCell value={registryUser(item)} /></td>
                      <td><StatePill label={item.configured ? (zh ? "已配置" : "configured") : (zh ? "缺失" : "missing")} value={item.configured ? "ready" : "missing"} /></td>
                      <td>
                        <button
                          className="ghost"
                          type="button"
                          disabled={busy !== ""}
                          onClick={() => void deleteRegistry(registryLabel(item))}
                        >
                          {busy === `remove-${registryLabel(item)}` ? (zh ? "删除中..." : "Removing...") : (zh ? "删除" : "Remove")}
                        </button>
                      </td>
                    </tr>
                  )) : (
                    <tr><td colSpan={4}>{state.loading ? (zh ? "读取中..." : "Loading...") : (zh ? "暂无 Registry 凭据" : "No registry credentials")}</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          ) : null}

          {activeTab === "git" ? (
            <div className="table-wrap">
              <table className="credentials-table">
                <thead>
                  <tr>
                    <th>{zh ? "Provider" : "Provider"}</th>
                    <th>{zh ? "账户" : "Account"}</th>
                    <th>{zh ? "Host" : "Host"}</th>
                    <th>{zh ? "状态" : "Status"}</th>
                    <th>{zh ? "操作" : "Actions"}</th>
                  </tr>
                </thead>
                <tbody>
                  {state.gitProviders.length ? state.gitProviders.map((item) => (
                    <tr key={gitProviderLabel(item)}>
                      <td><Badge value={gitProviderTypeLabel(item)} /></td>
                      <td><PrimaryCell title={item.account || "-"} meta={item.username || item.id} /></td>
                      <td><CodeCell value={item.cloneBaseUrl || item.baseUrl || "-"} /></td>
                      <td><StatePill label={item.configured ? (zh ? "已配置" : "configured") : (zh ? "缺失" : "missing")} value={item.configured ? "ready" : "missing"} /></td>
                      <td>
                        <button
                          className="ghost"
                          type="button"
                          disabled={busy !== ""}
                          onClick={() => void deleteGitProvider(gitProviderLabel(item))}
                        >
                          {busy === `remove-git-${gitProviderLabel(item)}` ? (zh ? "删除中..." : "Removing...") : (zh ? "删除" : "Remove")}
                        </button>
                      </td>
                    </tr>
                  )) : (
                    <tr><td colSpan={5}>{state.loading ? (zh ? "读取中..." : "Loading...") : (zh ? "暂无 Git provider 凭据" : "No Git provider credentials")}</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          ) : null}

          {activeTab === "storage" ? (
            <div className="table-wrap">
              <table className="credentials-table">
                <thead>
                  <tr>
                    <th>storageClass</th>
                    <th>provider</th>
                    <th>mode</th>
                    <th>{zh ? "节点/端点" : "Node / endpoint"}</th>
                    <th>regions</th>
                  </tr>
                </thead>
                <tbody>
                  {state.storageClasses.length ? state.storageClasses.map((item) => (
                    <tr key={item.name || "storage-class"}>
                      <td><PrimaryCell title={item.name || "-"} /></td>
                      <td><Badge value={item.provider || "-"} /></td>
                      <td><StatePill label={item.mode || "-"} value={item.mode === "external" ? "pending" : "ready"} /></td>
                      <td><CodeCell value={item.node || item.endpoint || item.path || "-"} /></td>
                      <td>{(item.regions || []).length ? item.regions?.map((region) => <Badge value={region} key={region} />) : "-"}</td>
                    </tr>
                  )) : (
                    <tr><td colSpan={5}>{state.loading ? (zh ? "读取中..." : "Loading...") : (zh ? "暂无 storageClass" : "No storage classes")}</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          ) : null}
        </article>

        {editing ? <article className="panel credentials-aside" style={{ maxWidth: 760 }}>
          {activeTab === "registries" ? (
            <>
              <div className="panel-heading">
                <div>
                  <p className="eyebrow">{zh ? "Registry 登录" : "Registry login"}</p>
                  <h2>{zh ? "保存拉取凭据" : "Save pull credentials"}</h2>
                </div>
                <PackageCheck size={18} aria-hidden="true" />
              </div>
              <div className="credential-form">
                <label className="field">
                  <span>{zh ? "Registry 主机" : "Registry host"}</span>
                  <input type="text" value={registryForm.host} placeholder="ghcr.io" onChange={(event) => setRegistryForm((current) => ({ ...current, host: event.target.value }))} />
                </label>
                <label className="field">
                  <span>{zh ? "用户名" : "Username"}</span>
                  <input type="text" autoComplete="off" value={registryForm.username} onChange={(event) => setRegistryForm((current) => ({ ...current, username: event.target.value }))} />
                </label>
                <label className="field">
                  <span>{zh ? "密码 / Token" : "Password / token"}</span>
                  <input type="password" autoComplete="new-password" value={registryForm.password} onChange={(event) => setRegistryForm((current) => ({ ...current, password: event.target.value }))} />
                </label>
                <button type="button" disabled={busy !== "" || !registryForm.host.trim() || !registryForm.username.trim() || !registryForm.password} onClick={() => void submitRegistry()}>
                  <ShieldCheck size={16} aria-hidden="true" />
                  {busy === "registry" ? (zh ? "保存中..." : "Saving...") : (zh ? "保存凭据" : "Save credential")}
                </button>
                <p className="credential-hint">{zh ? "用于拉取私有镜像。值保存后不回显。等价于 luma registry login。" : "Used to pull private images. The value is not echoed back. Equivalent to luma registry login."}</p>
              </div>
            </>
          ) : activeTab === "git" ? (
            <>
              <div className="panel-heading">
                <div>
                  <p className="eyebrow">{zh ? "Git provider" : "Git provider"}</p>
                  <h2>{zh ? "保存仓库访问 token" : "Save repository token"}</h2>
                </div>
                <GitBranch size={18} aria-hidden="true" />
              </div>
              <div className="credential-form">
                <label className="field">
                  <span>{zh ? "Provider" : "Provider"}</span>
                  <SelectControl
                    value={gitProviderForm.type}
                    onChange={(value) => setGitProviderForm((current) => ({ ...current, type: value }))}
                    options={[
                      { value: "github", label: "GitHub" },
                      { value: "gitea", label: zh ? "Git / Gitea" : "Git / Gitea" },
                    ]}
                  />
                </label>
                <label className="field">
                  <span>{zh ? "账户名称" : "Account name"}</span>
                  <input type="text" value={gitProviderForm.account} placeholder={gitProviderForm.type === "github" ? "personal" : "work"} autoComplete="off" onChange={(event) => setGitProviderForm((current) => ({ ...current, account: event.target.value }))} />
                </label>
                <label className="field">
                  <span>{zh ? "用户名（可选）" : "Username (optional)"}</span>
                  <input type="text" value={gitProviderForm.username} autoComplete="off" onChange={(event) => setGitProviderForm((current) => ({ ...current, username: event.target.value }))} />
                </label>
                {gitProviderForm.type === "gitea" ? (
                  <>
                    <label className="field">
                      <span>Base URL</span>
                      <input type="text" value={gitProviderForm.baseUrl} placeholder="https://gcode.example.com" onChange={(event) => setGitProviderForm((current) => ({ ...current, baseUrl: event.target.value }))} />
                    </label>
                    <label className="field">
                      <span>Clone base URL</span>
                      <input type="text" value={gitProviderForm.cloneBaseUrl} placeholder={zh ? "留空同 Base URL" : "blank = Base URL"} onChange={(event) => setGitProviderForm((current) => ({ ...current, cloneBaseUrl: event.target.value }))} />
                    </label>
                  </>
                ) : null}
                <label className="field">
                  <span>{zh ? "Token / PAT" : "Token / PAT"}</span>
                  <input type="password" autoComplete="new-password" value={gitProviderForm.token} onChange={(event) => setGitProviderForm((current) => ({ ...current, token: event.target.value }))} />
                </label>
                <button
                  type="button"
                  disabled={
                    busy !== "" ||
                    !gitProviderForm.account.trim() ||
                    !gitProviderForm.token ||
                    (gitProviderForm.type === "gitea" && !gitProviderForm.baseUrl.trim())
                  }
                  onClick={() => void submitGitProvider()}
                >
                  <ShieldCheck size={16} aria-hidden="true" />
                  {busy === "git-provider" ? (zh ? "保存中..." : "Saving...") : (zh ? "保存 Git 凭据" : "Save Git credential")}
                </button>
                <p className="credential-hint">{zh ? "同一 provider 可保存多个账户。Token 只写不回显，导入仓库时按账户选择注入。" : "You can save multiple accounts per provider. Tokens are write-only and injected only for the selected import account."}</p>
              </div>
            </>
          ) : (
            <>
              <div className="panel-heading">
                <div>
                  <p className="eyebrow">{zh ? "新增 / 轮换 Secret" : "Add / rotate secret"}</p>
                  <h2>{zh ? "写入控制面密钥" : "Write a control-plane secret"}</h2>
                </div>
                <KeyRound size={18} aria-hidden="true" />
              </div>
              <div className="credential-form">
                <label className="field">
                  <span>{zh ? "名称" : "Name"}</span>
                  <input type="text" value={secretForm.name} placeholder="DATABASE_URL" autoComplete="off" onChange={(event) => setSecretForm((current) => ({ ...current, name: event.target.value }))} />
                </label>
                <label className="field">
                  <span>{zh ? "作用域（可选）" : "Scope (optional)"}</span>
                  <input type="text" value={secretForm.scope} placeholder={zh ? "留空为全局；或填应用名" : "blank = global; or an app name"} autoComplete="off" onChange={(event) => setSecretForm((current) => ({ ...current, scope: event.target.value }))} />
                </label>
                <label className="field">
                  <span>{zh ? "值" : "Value"}</span>
                  <input type="password" autoComplete="new-password" value={secretForm.value} onChange={(event) => setSecretForm((current) => ({ ...current, value: event.target.value }))} />
                </label>
                <button type="button" disabled={busy !== "" || !secretForm.name.trim() || !secretForm.value} onClick={() => void submitSecret()}>
                  <ShieldCheck size={16} aria-hidden="true" />
                  {busy === "secret" ? (zh ? "保存中..." : "Saving...") : (zh ? "保存 Secret" : "Save secret")}
                </button>
                <p className="credential-hint">{zh ? "同名保存即轮换。值保存后不回显；删除只影响后续部署，不会修改已经运行的实例。GitHub 私有仓库导入用名称 GITHUB_TOKEN。" : "Saving the same name rotates it. Values are never echoed back; removal affects future deployments and does not modify running instances. For private GitHub imports use the name GITHUB_TOKEN."}</p>
              </div>
            </>
          )}
        </article> : null}
      </section>
      {confirmDialog}
    </>
  );
}
