import { useEffect, useMemo, useState } from "react";
import { KeyRound, LockKeyhole, PackageCheck, ShieldAlert } from "lucide-react";
import { fetchRegistries, fetchSecrets, fetchStorageClasses, type RegistryCredential } from "../controlResourcesApi";
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

  useEffect(() => {
    const controller = new AbortController();
    setState((current) => ({ ...current, loading: true, error: "" }));
    Promise.all([
      fetchSecrets({ token, signal: controller.signal }),
      fetchRegistries({ token, signal: controller.signal }),
      fetchStorageClasses({ token, signal: controller.signal }),
    ])
      .then(([secrets, registries, storage]) => {
        setState({
          secrets: secrets.secrets || [],
          registries: registries.registries || [],
          storageClasses: storage.storageClasses || vm.storageClasses,
          loading: false,
          error: "",
        });
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setState((current) => ({
          ...current,
          loading: false,
          error: String(error instanceof Error ? error.message : error),
        }));
      });
    return () => controller.abort();
  }, [token, vm.storageClasses]);

  const parsedSecrets = useMemo(() => state.secrets.map(parseSecretName), [state.secrets]);
  const scopedSecrets = parsedSecrets.filter((item) => item.scope !== "global").length;
  const configuredRegistries = state.registries.filter((item) => item.configured).length;

  return (
    <>
      <PageHeader
        meta={{
          eyebrow: zh ? "凭据" : "Credentials",
          title: zh ? "Secret、Registry 与存储凭据" : "Secrets, registries, and storage credentials",
          description: zh
            ? "读取控制面已登记的敏感配置索引；值保持 write-only，不在浏览器回显。写入流程本轮先保留为 CLI/API 提示。"
            : "Read the control-plane sensitive-config index. Secret values stay write-only and are never echoed back to the browser. Write flows remain CLI/API guided for now.",
          metrics: [
            { label: zh ? "Secrets" : "Secrets", value: state.secrets.length },
            { label: zh ? "Scoped" : "Scoped", value: scopedSecrets },
            { label: zh ? "Registries" : "Registries", value: configuredRegistries },
            { label: "storageClass", value: state.storageClasses.length },
          ],
          action: (
            <button disabled type="button" title={zh ? "本轮只读展示，新增 secret 请继续使用 CLI。" : "This iteration is read-only. Add secrets through the CLI for now."}>
              <KeyRound size={16} aria-hidden="true" />
              {zh ? "新增 Secret" : "Add secret"}
            </button>
          ),
        }}
      />

      {state.error ? (
        <div className="storage-warnings credentials-warning">
          <span>{state.error}</span>
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
                    <th>{zh ? "操作" : "Actions"}</th>
                  </tr>
                </thead>
                <tbody>
                  {parsedSecrets.length ? parsedSecrets.map((secret) => (
                    <tr key={`${secret.scope}/${secret.name}`}>
                      <td><PrimaryCell title={secret.name} /></td>
                      <td><Badge value={secretScopeLabel(secret.scope, lang)} /></td>
                      <td><CodeCell value="write-only" /></td>
                      <td><StatePill label={zh ? "已保存" : "saved"} value="ready" /></td>
                      <td><button className="ghost" type="button" disabled>{zh ? "轮换" : "Rotate"}</button></td>
                    </tr>
                  )) : (
                    <tr><td colSpan={5}>{state.loading ? (zh ? "读取中..." : "Loading...") : (zh ? "暂无 Secret" : "No secrets")}</td></tr>
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
                      <td><button className="ghost" type="button" disabled>{zh ? "重新登录" : "Login"}</button></td>
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
          <div className="panel-heading">
            <div>
              <p className="eyebrow">{zh ? "写操作状态" : "Write status"}</p>
              <h2>{zh ? "本轮只读" : "Read-only in this pass"}</h2>
            </div>
            <ShieldAlert size={18} aria-hidden="true" />
          </div>
          <div className="credential-command-stack">
            <div>
              <strong>{zh ? "新增 Secret" : "Add secret"}</strong>
              <code>luma secret set DATABASE_URL --scope app</code>
            </div>
            <div>
              <strong>{zh ? "Registry 登录" : "Registry login"}</strong>
              <code>luma registry login ghcr.io --username user --password-stdin</code>
            </div>
            <div>
              <strong>{zh ? "存储类" : "Storage class"}</strong>
              <code>luma storage set home-nfs --provider nfs --endpoint nas:/srv/luma</code>
            </div>
          </div>
          <p>{zh ? "这些命令会改变控制面状态。本次前端先保证展示、审计语义和敏感值不回显。" : "These commands mutate control-plane state. This frontend pass focuses on display, audit semantics, and never echoing secret values."}</p>
        </aside>
      </section>
    </>
  );
}
