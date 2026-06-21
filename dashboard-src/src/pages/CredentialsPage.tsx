import { useCallback, useEffect, useMemo, useState } from "react";
import { KeyRound, LockKeyhole, PackageCheck, ShieldCheck } from "lucide-react";
import { fetchRegistries, fetchSecrets, fetchStorageClasses, removeRegistry, setRegistry, setSecret, type RegistryCredential } from "../controlResourcesApi";
import { Badge, CodeCell, PrimaryCell, StatePill } from "../components/ui";
import type { DashboardStorageClass, Lang } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";

type CredentialsState = {
  secrets: string[];
  registries: RegistryCredential[];
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

function secretScopeLabel(scope: string, lang: Lang) {
  if (scope === "global") return lang === "zh" ? "全局" : "global";
  return scope;
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
  const [activeTab, setActiveTab] = useState<"secrets" | "registries" | "storage">("secrets");
  const [state, setState] = useState<CredentialsState>({
    secrets: [],
    registries: [],
    storageClasses: vm.storageClasses,
    loading: true,
    error: "",
  });

  // Write-form state. Sensitive values live only in the form fields and are
  // cleared right after a successful submit; they are never persisted to the
  // read state and never rendered back.
  const [secretForm, setSecretForm] = useState({ name: "", scope: "", value: "" });
  const [registryForm, setRegistryForm] = useState({ host: "", username: "", password: "" });
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [writeError, setWriteError] = useState("");

  const refresh = useCallback(async (signal?: AbortSignal) => {
    setState((current) => ({ ...current, loading: true, error: "" }));
    try {
      const [secrets, registries, storage] = await Promise.all([
        fetchSecrets({ token, signal }),
        fetchRegistries({ token, signal }),
        fetchStorageClasses({ token, signal }),
      ]);
      setState({
        secrets: secrets.secrets || [],
        registries: registries.registries || [],
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
  const scopedSecrets = parsedSecrets.filter((item) => item.scope !== "global").length;
  const configuredRegistries = state.registries.filter((item) => item.configured).length;

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
            <button type="button" className={activeTab === "storage" ? "active" : ""} onClick={() => setActiveTab("storage")}>
              storageClass
            </button>
          </div>

          {activeTab === "secrets" ? (
            <div className="table-wrap">
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
                  {parsedSecrets.length ? parsedSecrets.map((secret) => (
                    <tr key={`${secret.scope}/${secret.name}`}>
                      <td><PrimaryCell title={secret.name} /></td>
                      <td><Badge value={secretScopeLabel(secret.scope, lang)} /></td>
                      <td><CodeCell value="write-only" /></td>
                      <td><StatePill label={zh ? "已保存" : "saved"} value="ready" /></td>
                    </tr>
                  )) : (
                    <tr><td colSpan={4}>{state.loading ? (zh ? "读取中..." : "Loading...") : (zh ? "暂无 Secret" : "No secrets")}</td></tr>
                  )}
                </tbody>
              </table>
            </div>
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
