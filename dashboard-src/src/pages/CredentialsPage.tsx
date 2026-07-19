import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, GitBranch, KeyRound, LockKeyhole, PackageCheck, ShieldCheck } from "lucide-react";
import {
  fetchGitProviders,
  fetchRegistries,
  fetchSecrets,
  fetchStorageClasses,
  removeGitProvider,
  removeRegistry,
  setGitProvider,
  setRegistry,
  setSecret,
  type GitProviderCredential,
  type RegistryCredential,
} from "../controlResourcesApi";
import { Badge, CodeCell, PrimaryCell, SelectControl, StatePill } from "../components/ui";
import type { DashboardStorageClass, Lang } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";

type CredentialsState = {
  secrets: string[];
  registries: RegistryCredential[];
  gitProviders: GitProviderCredential[];
  storageClasses: DashboardStorageClass[];
  loading: boolean;
  error: string;
};

function parseSecretName(value: string) {
  const [maybeScope, maybeName] = value.includes("/") ? value.split("/", 2) : ["global", value];
  return { scope: maybeName ? maybeScope : "global", name: maybeName || maybeScope };
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
  if (scope === "global") return lang === "zh" ? "全局" : "global";
  return scope;
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
    if (secret.scope !== "global") {
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
  const [activeTab, setActiveTab] = useState<"secrets" | "registries" | "git" | "storage">("secrets");
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

  const parsedSecrets = useMemo(() => state.secrets.map(parseSecretName), [state.secrets]);
  const secretGroups = useMemo(() => buildSecretGroups(parsedSecrets, lang), [lang, parsedSecrets]);
  const scopedSecrets = parsedSecrets.filter((item) => item.scope !== "global").length;
  const configuredRegistries = state.registries.filter((item) => item.configured).length;
  const configuredGitProviders = state.gitProviders.filter((item) => item.configured).length;

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
    } catch (error) {
      setWriteError(String(error instanceof Error ? error.message : error));
    } finally {
      setBusy("");
    }
  };

  const deleteRegistry = async (host: string) => {
    if (!host || host === "-") return;
    if (!window.confirm(zh ? `删除 ${host} 的 registry 凭据？` : `Remove registry credential for ${host}?`)) return;
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
    } catch (error) {
      setWriteError(String(error instanceof Error ? error.message : error));
    } finally {
      setBusy("");
    }
  };

  const deleteGitProvider = async (id: string) => {
    if (!id || id === "-") return;
    if (!window.confirm(zh ? `删除 ${id} 的 Git 凭据？` : `Remove Git credential for ${id}?`)) return;
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
          eyebrow: zh ? "凭据" : "Credentials",
          title: zh ? "Secret、Registry 与存储凭据" : "Secrets, registries, and storage credentials",
          description: zh
            ? "管理控制面的敏感配置。值只写不回显：保存后不会再返回浏览器。"
            : "Manage control-plane sensitive config. Values are write-only and never returned to the browser after saving.",
          metrics: [
            { label: zh ? "Secrets" : "Secrets", value: state.secrets.length },
            { label: zh ? "Scoped" : "Scoped", value: scopedSecrets },
            { label: zh ? "Registries" : "Registries", value: configuredRegistries },
            { label: zh ? "Git accounts" : "Git accounts", value: configuredGitProviders },
            { label: "storageClass", value: state.storageClasses.length },
          ],
        }}
      />

      {state.error ? (
        <div className="storage-warnings credentials-warning">
          <span>{state.error}</span>
        </div>
      ) : null}
      {notice ? (
        <div className="storage-warnings credentials-notice">
          <span>{notice}</span>
        </div>
      ) : null}
      {writeError ? (
        <div className="storage-warnings credentials-warning">
          <span>{writeError}</span>
        </div>
      ) : null}

      <section className="credentials-layout">
        <article className="panel credentials-index-panel">
          <div className="credentials-tabs" role="tablist" aria-label={zh ? "凭据视图" : "Credential views"}>
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
              storageClass
            </button>
          </div>

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
                              </tr>
                            </thead>
                            <tbody>
                              {group.secrets.map((secret) => (
                                <tr key={`${secret.scope}/${secret.name}`}>
                                  <td><PrimaryCell title={secret.name} /></td>
                                  <td><Badge value={secretScopeLabel(secret.scope, lang)} /></td>
                                  <td><CodeCell value="write-only" /></td>
                                  <td><StatePill label={zh ? "已保存" : "saved"} value="ready" /></td>
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

        <aside className="panel credentials-aside">
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
                <p className="credential-hint">{zh ? "同名保存即轮换。值保存后不回显，也不能从这里删除（如需删除请用 CLI）。GitHub 私有仓库导入用名称 GITHUB_TOKEN。" : "Saving the same name rotates it. Values are not echoed back and cannot be deleted here (use the CLI). For private GitHub imports use the name GITHUB_TOKEN."}</p>
              </div>
            </>
          )}
        </aside>
      </section>
    </>
  );
}
