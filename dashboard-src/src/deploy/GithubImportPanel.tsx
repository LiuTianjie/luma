import { ArrowLeft, GitBranch, Rocket, RotateCcw, Server, Settings2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  fetchGitProviderRefs,
  fetchGitProviderRepositories,
  fetchGitProviders,
  type GitProviderCredential,
  type GitRef,
  type GitRepository,
} from "../controlResourcesApi";
import type { DashboardBuildNode, DashboardNode, Lang } from "../types";
import { buildImportStream, fetchBuildRun, fetchBuildRuns, registryServeStream, retryBuildRun, type BuildRun } from "./deployApi";
import { isReadyNode } from "./options";
import type { DeployStep, Exposure, Region } from "./types";

const REGIONS: Region[] = ["cn", "global", "home"];
const EXPOSURES: Exposure[] = ["none", "cn-edge", "external-edge", "tailscale-relay", "cloudflare-tunnel", "tcp-relay"];
const PROVIDER_TYPES = ["github", "gitea"] as const;

function buildNodes(nodes: DashboardNode[], declared: DashboardBuildNode[] = []): DashboardNode[] {
  const declaredNames = declared.filter((node) => node.ready && node.name).map((node) => node.name as string);
  if (declaredNames.length) {
    const byName = new Map(nodes.map((node) => [node.name, node]));
    return declaredNames.map((name) => byName.get(name) || { name }).filter((node) => Boolean(node.name));
  }
  return nodes.filter((node) => isReadyNode(node) && (node.storageCapabilities || []).includes("docker-build"));
}

function providerTypeLabel(type: string, lang: Lang) {
  if (type === "github") return "GitHub";
  return lang === "zh" ? "Git / Gitea" : "Git / Gitea";
}

function providerAccountLabel(provider: GitProviderCredential) {
  const account = provider.account || provider.id || "-";
  const username = provider.username ? ` (${provider.username})` : "";
  return `${account}${username}`;
}

function repoOptionLabel(repo: GitRepository) {
  const privacy = repo.private ? " private" : "";
  const branch = repo.defaultBranch ? ` - ${repo.defaultBranch}` : "";
  return `${repo.fullName}${branch}${privacy}`;
}

function parseEnvText(text: string): { values: Record<string, string>; errors: string[] } {
  const values: Record<string, string> = {};
  const errors: string[] = [];
  text.split(/\r?\n/).forEach((rawLine, index) => {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) return;
    const eq = line.indexOf("=");
    if (eq <= 0) {
      errors.push(`line ${index + 1}: expected KEY=VALUE`);
      return;
    }
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
      errors.push(`line ${index + 1}: invalid env name`);
      return;
    }
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    values[key] = value;
  });
  return { values, errors };
}

export function GithubImportPanel({
  lang,
  token,
  nodes,
  build,
  onBack,
  onRefresh,
}: {
  lang: Lang;
  token: string;
  nodes: DashboardNode[];
  build?: {
    defaultNode?: string;
    registryHost?: string;
    pushHost?: string;
    nodes?: DashboardBuildNode[];
  };
  onBack?: () => void;
  onRefresh: () => Promise<void> | void;
}) {
  const zh = lang === "zh";
  const candidates = useMemo(() => buildNodes(nodes, build?.nodes || []), [build?.nodes, nodes]);
  const preferredBuildNode = useMemo(() => {
    const defaultNode = build?.defaultNode || "";
    return candidates.some((node) => node.name === defaultNode) ? defaultNode : candidates[0]?.name || "";
  }, [build?.defaultNode, candidates]);

  const [mode, setMode] = useState<"provider" | "manual">("provider");
  const [providerType, setProviderType] = useState<(typeof PROVIDER_TYPES)[number]>("github");
  const [providers, setProviders] = useState<GitProviderCredential[]>([]);
  const [providerId, setProviderId] = useState("");
  const [repositories, setRepositories] = useState<GitRepository[]>([]);
  const [repository, setRepository] = useState("");
  const [refs, setRefs] = useState<GitRef[]>([]);
  const [repoUrl, setRepoUrl] = useState("");
  const [providerLoading, setProviderLoading] = useState(false);
  const [repositoryLoading, setRepositoryLoading] = useState(false);
  const [refLoading, setRefLoading] = useState(false);
  const [sourceError, setSourceError] = useState("");

  const [buildNode, setBuildNode] = useState(preferredBuildNode);
  const [ref, setRef] = useState("");
  const [region, setRegion] = useState<Region | "">("");
  const [exposure, setExposure] = useState<Exposure | "">("");
  const [domain, setDomain] = useState("");
  const [port, setPort] = useState("");
  const [manifest, setManifest] = useState("");
  const [envText, setEnvText] = useState("");
  const [platform, setPlatform] = useState("");
  const [registryHost, setRegistryHost] = useState("");
  const [pushHost, setPushHost] = useState("");
  const [context, setContext] = useState("");
  const [dockerfile, setDockerfile] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [steps, setSteps] = useState<DeployStep[]>([]);
  const [status, setStatus] = useState<"idle" | "running">("idle");
  const [error, setError] = useState("");

  const [showRegistry, setShowRegistry] = useState(false);
  const [registryNode, setRegistryNode] = useState(preferredBuildNode);
  const [registrySteps, setRegistrySteps] = useState<DeployStep[]>([]);
  const [registryStatus, setRegistryStatus] = useState<"idle" | "running">("idle");
  const [registryError, setRegistryError] = useState("");
  const [registryDone, setRegistryDone] = useState("");
  const [buildRuns, setBuildRuns] = useState<BuildRun[]>([]);
  const [selectedRun, setSelectedRun] = useState<BuildRun | null>(null);
  const [runsLoading, setRunsLoading] = useState(false);
  const [runAction, setRunAction] = useState("");

  const loadBuildRuns = async () => {
    setRunsLoading(true);
    try {
      const payload = await fetchBuildRuns(token);
      setBuildRuns(payload.runs || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunsLoading(false);
    }
  };

  useEffect(() => {
    if (!buildNode && preferredBuildNode) setBuildNode(preferredBuildNode);
    if (!registryNode && preferredBuildNode) setRegistryNode(preferredBuildNode);
  }, [buildNode, preferredBuildNode, registryNode]);

  useEffect(() => {
    void loadBuildRuns();
  }, [token]);

  useEffect(() => {
    const controller = new AbortController();
    setProviderLoading(true);
    setSourceError("");
    fetchGitProviders({ token, signal: controller.signal })
      .then((payload) => {
        setProviders(payload.providers || []);
        if (!(payload.providers || []).length) setMode("manual");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setSourceError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!controller.signal.aborted) setProviderLoading(false);
      });
    return () => controller.abort();
  }, [token]);

  const accounts = useMemo(() => providers.filter((provider) => provider.type === providerType), [providers, providerType]);

  useEffect(() => {
    const first = accounts[0]?.id || "";
    if (!accounts.some((provider) => provider.id === providerId)) {
      setProviderId(first);
      setRepository("");
      setRepositories([]);
      setRefs([]);
      setRef("");
    }
  }, [accounts, providerId]);

  useEffect(() => {
    if (!providerId || mode !== "provider") return;
    const controller = new AbortController();
    setRepositoryLoading(true);
    setSourceError("");
    fetchGitProviderRepositories({ token, providerId, signal: controller.signal })
      .then((payload) => {
        const items = payload.repositories || [];
        setRepositories(items);
        const selected = items.find((item) => item.fullName === repository) || items[0];
        setRepository(selected?.fullName || "");
        setRef(selected?.defaultBranch || "");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setSourceError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!controller.signal.aborted) setRepositoryLoading(false);
      });
    return () => controller.abort();
  }, [mode, providerId, token]);

  useEffect(() => {
    if (!providerId || !repository || mode !== "provider") return;
    const controller = new AbortController();
    setRefLoading(true);
    setSourceError("");
    fetchGitProviderRefs({ token, providerId, repository, signal: controller.signal })
      .then((payload) => {
        const items = payload.refs || [];
        setRefs(items);
        if (!items.some((item) => item.name === ref)) {
          const defaultBranch = repositories.find((item) => item.fullName === repository)?.defaultBranch || "";
          setRef(defaultBranch || items[0]?.name || "");
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) setSourceError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!controller.signal.aborted) setRefLoading(false);
      });
    return () => controller.abort();
  }, [mode, providerId, ref, repositories, repository, token]);

  const errors = useMemo(() => {
    const list: string[] = [];
    const parsedEnv = parseEnvText(envText);
    if (mode === "provider") {
      if (!providerId) list.push(zh ? "请选择 Git 账户凭据" : "Select a Git account credential");
      if (!repository) list.push(zh ? "请选择仓库" : "Select a repository");
    } else if (!repoUrl.trim()) {
      list.push(zh ? "仓库地址不能为空" : "Repository URL is required");
    }
    if (!buildNode) list.push(zh ? "必须选择一个构建节点（需具备 docker-build 能力）" : "Select a build node (must have docker-build capability)");
    if (exposure && exposure !== "none" && !domain.trim()) list.push(zh ? "公开入口必须填写域名" : "Public exposure requires a domain");
    if (port.trim() && !/^[0-9]+$/.test(port.trim())) list.push(zh ? "端口必须是正整数" : "Port must be a positive integer");
    for (const message of parsedEnv.errors) list.push(zh ? `环境变量 ${message}` : `Environment ${message}`);
    return list;
  }, [buildNode, domain, envText, exposure, mode, port, providerId, repoUrl, repository, zh]);

  const run = async () => {
    if (errors.length) return;
    const parsedEnv = parseEnvText(envText);
    setStatus("running");
    setSteps([]);
    setError("");
    try {
      await buildImportStream(
        {
          token,
          repoUrl: mode === "manual" ? repoUrl.trim() : undefined,
          providerId: mode === "provider" ? providerId : undefined,
          repository: mode === "provider" ? repository : undefined,
          buildNode,
          ref: ref.trim(),
          region: region || undefined,
          exposure: exposure || undefined,
          domain: domain.trim(),
          port: port.trim(),
          manifest: manifest.trim(),
          platform: platform.trim(),
          registryHost: registryHost.trim(),
          pushHost: pushHost.trim(),
          context: context.trim(),
          dockerfile: dockerfile.trim(),
          envSecrets: Object.keys(parsedEnv.values).length ? parsedEnv.values : undefined,
        },
        (step) => setSteps((current) => [...current, step]),
      );
      await loadBuildRuns();
      await onRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setStatus("idle");
    }
  };

  const openBuildRun = async (id?: string) => {
    if (!id) return;
    setRunAction(id);
    try {
      const payload = await fetchBuildRun(token, id);
      setSelectedRun(payload.run || null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunAction("");
    }
  };

  const retryRun = async (id?: string) => {
    if (!id) return;
    setRunAction(id);
    setError("");
    try {
      await retryBuildRun(token, id);
      await loadBuildRuns();
      if (selectedRun?.id === id) await openBuildRun(id);
      await onRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunAction("");
    }
  };

  const runRegistry = async () => {
    if (!registryNode) return;
    setRegistryStatus("running");
    setRegistrySteps([]);
    setRegistryError("");
    setRegistryDone("");
    try {
      const result = (await registryServeStream(
        { token, node: registryNode },
        (step) => setRegistrySteps((current) => [...current, step]),
      )) as { registryHost?: string } | null;
      setRegistryDone(result?.registryHost ? (zh ? `registry 就绪：${result.registryHost}` : `registry ready: ${result.registryHost}`) : (zh ? "registry 已部署" : "registry deployed"));
      await onRefresh();
    } catch (err) {
      setRegistryError(err instanceof Error ? err.message : String(err));
    } finally {
      setRegistryStatus("idle");
    }
  };

  return (
    <>
      <div className="panel-heading deploy-heading">
        <div>
          <p className="eyebrow">{zh ? "仓库导入" : "Repository import"}</p>
          <h2>{zh ? "从 Git provider 构建并部署" : "Build and deploy from a Git provider"}</h2>
          <small className="deploy-context-label">
            {zh
              ? "选择 GitHub 或 Gitea 账户、仓库和分支，在声明的构建节点构建镜像并推送到内部 registry。"
              : "Choose a GitHub or Gitea account, repository, and ref; build on a declared builder node and push to the internal registry."}
          </small>
        </div>
        <div className="deploy-heading-actions">
          {onBack ? (
            <button type="button" className="ghost" onClick={onBack}>
              <ArrowLeft size={16} aria-hidden="true" />
              {zh ? "返回模板" : "Back to templates"}
            </button>
          ) : null}
        </div>
      </div>

      <div className="deploy-form-stack">
        <section className="deploy-config-section">
          <header><span>01</span><h3>{zh ? "代码来源" : "Source repository"}</h3></header>
          <div className="credentials-tabs repository-source-tabs" role="tablist" aria-label={zh ? "仓库来源" : "Repository source"}>
            <button type="button" className={mode === "provider" ? "active" : ""} onClick={() => setMode("provider")} disabled={!providers.length && !providerLoading}>
              <GitBranch size={15} aria-hidden="true" />
              {zh ? "已托管凭据" : "Saved providers"}
            </button>
            <button type="button" className={mode === "manual" ? "active" : ""} onClick={() => setMode("manual")}>
              {zh ? "手填 URL" : "Repo URL"}
            </button>
          </div>

          {mode === "provider" ? (
            <div className="deploy-field-grid">
              <label>
                <span>{zh ? "Git provider" : "Git provider"}</span>
                <select value={providerType} onChange={(event) => setProviderType(event.target.value as (typeof PROVIDER_TYPES)[number])}>
                  {PROVIDER_TYPES.map((type) => <option key={type} value={type}>{providerTypeLabel(type, lang)}</option>)}
                </select>
              </label>
              <label>
                <span>{zh ? "账户凭据" : "Account credential"}</span>
                <select value={providerId} onChange={(event) => setProviderId(event.target.value)}>
                  <option value="">{providerLoading ? (zh ? "读取中..." : "Loading...") : (zh ? "选择账户" : "Select account")}</option>
                  {accounts.map((provider) => (
                    <option key={provider.id} value={provider.id}>{providerAccountLabel(provider)}</option>
                  ))}
                </select>
                {!accounts.length && !providerLoading ? <small className="deploy-muted">{zh ? "先在 Credentials / Git Providers 添加这个 provider 的账户 token。" : "Add an account token in Credentials / Git Providers first."}</small> : null}
              </label>
              <label className="deploy-field-wide">
                <span>{zh ? "仓库" : "Repository"}</span>
                <select value={repository} onChange={(event) => setRepository(event.target.value)} disabled={!providerId || repositoryLoading}>
                  <option value="">{repositoryLoading ? (zh ? "读取仓库中..." : "Loading repositories...") : (zh ? "选择仓库" : "Select repository")}</option>
                  {repositories.map((repo) => (
                    <option key={repo.fullName} value={repo.fullName}>{repoOptionLabel(repo)}</option>
                  ))}
                </select>
              </label>
              <label>
                <span>{zh ? "分支 / Tag" : "Branch / tag"}</span>
                <select value={ref} onChange={(event) => setRef(event.target.value)} disabled={!repository || refLoading}>
                  <option value="">{refLoading ? (zh ? "读取 refs 中..." : "Loading refs...") : (zh ? "默认分支" : "Default branch")}</option>
                  {refs.map((item) => (
                    <option key={`${item.type}-${item.name}`} value={item.name}>{item.name} ({item.type})</option>
                  ))}
                </select>
              </label>
            </div>
          ) : (
            <div className="deploy-field-grid">
              <label className="deploy-field-wide">
                <span>{zh ? "仓库 URL" : "Repository URL"}</span>
                <input type="text" value={repoUrl} placeholder="https://github.com/owner/repo" onChange={(event) => setRepoUrl(event.target.value)} />
              </label>
              <label>
                <span>{zh ? "分支 / Tag（可选）" : "Branch / tag (optional)"}</span>
                <input type="text" value={ref} placeholder="main" onChange={(event) => setRef(event.target.value)} />
              </label>
            </div>
          )}
          {sourceError ? <div className="deploy-muted">{sourceError}</div> : null}
        </section>

        <section className="deploy-config-section">
          <header><span>02</span><h3>{zh ? "构建目标" : "Build target"}</h3></header>
          <div className="deploy-field-grid">
            <label>
              <span>{zh ? "构建节点" : "Build node"}</span>
              <select value={buildNode} onChange={(event) => setBuildNode(event.target.value)}>
                <option value="">{zh ? "选择构建节点" : "Select build node"}</option>
                {candidates.map((node) => (
                  <option key={node.name} value={node.name}>{node.displayName || node.name}</option>
                ))}
              </select>
              {!candidates.length ? <small className="deploy-muted">{zh ? "当前没有可用的声明构建节点，节点需具备 docker-build 能力。" : "No declared builder node is currently available; the node must advertise docker-build."}</small> : null}
            </label>
          </div>
          <div className="registry-setup">
            <button type="button" className="ghost registry-setup-toggle" onClick={() => setShowRegistry((current) => !current)}>
              <Server size={15} aria-hidden="true" />
              {zh ? "内部 registry 设置" : "Internal registry setup"}
              <span>{showRegistry ? "▾" : "▸"}</span>
            </button>
            {showRegistry ? (
              <div className="registry-setup-body">
                <div className="deploy-field-grid">
                  <label>
                    <span>{zh ? "registry 所在节点" : "Registry node"}</span>
                    <select value={registryNode} onChange={(event) => setRegistryNode(event.target.value)}>
                      <option value="">{zh ? "选择节点" : "Select node"}</option>
                      {candidates.map((node) => (
                        <option key={node.name} value={node.name}>{node.displayName || node.name}</option>
                      ))}
                    </select>
                    {!candidates.length ? <small className="deploy-muted">{zh ? "内部 registry 需要部署到已声明且可用的构建节点。" : "Internal registry setup needs a declared, available builder node."}</small> : null}
                  </label>
                  <button type="button" disabled={registryStatus !== "idle" || !registryNode} onClick={() => void runRegistry()}>
                    <Server size={15} aria-hidden="true" />
                    {registryStatus === "running" ? (zh ? "部署中..." : "Deploying...") : (zh ? "部署 registry" : "Deploy registry")}
                  </button>
                </div>
                {registryDone ? <small className="deploy-muted registry-done">{registryDone}</small> : null}
                {registryError ? <div className="deploy-muted registry-error">{registryError}</div> : null}
                {registrySteps.length ? (
                  <ol className="deploy-step-log">
                    {registrySteps.filter((step) => step.name).map((step, index) => (
                      <li key={`${step.name}-${index}`} className={`step-${step.status || "ok"}`}>
                        <strong>{step.name}</strong>
                        {step.message ? <span> - {step.message}</span> : null}
                      </li>
                    ))}
                  </ol>
                ) : null}
              </div>
            ) : null}
          </div>
        </section>

        <section className="deploy-config-section">
          <header><span>03</span><h3>{zh ? "部署覆盖项" : "Deploy overrides"}</h3></header>
          <div className="deploy-field-grid">
            <label>
              <span>Region</span>
              <select value={region} onChange={(event) => setRegion(event.target.value as Region | "")}>
                <option value="">{zh ? "用仓库配置" : "from repo"}</option>
                {REGIONS.map((value) => <option key={value} value={value}>{value}</option>)}
              </select>
            </label>
            <label>
              <span>Exposure</span>
              <select value={exposure} onChange={(event) => setExposure(event.target.value as Exposure | "")}>
                <option value="">{zh ? "用仓库配置" : "from repo"}</option>
                {EXPOSURES.map((value) => <option key={value} value={value}>{value}</option>)}
              </select>
            </label>
            <label>
              <span>{zh ? "域名" : "Domain"}</span>
              <input type="text" value={domain} placeholder="app.example.com" onChange={(event) => setDomain(event.target.value)} />
            </label>
            <label>
              <span>{zh ? "端口" : "Port"}</span>
              <input type="text" value={port} placeholder="8080" onChange={(event) => setPort(event.target.value)} />
            </label>
          </div>
          <label className="deploy-field-wide deploy-manifest-field">
            <span>{zh ? "Luma 部署文件（可选）" : "Luma manifest (optional)"}</span>
            <textarea
              value={manifest}
              onChange={(event) => setManifest(event.target.value)}
              placeholder={"name: app\nimage: placeholder\nregion: cn\nexposure: none"}
              spellCheck={false}
            />
            <small className="deploy-muted">
              {zh
                ? "仓库里有 Luma service 或 Compose 部署文件时会自动使用；这里填写后可作为没有部署文件时的手动输入。"
                : "If the repository has a Luma service or Compose deployment file, Luma uses it automatically; fill this when the repo has no manifest yet."}
            </small>
          </label>
          <label className="deploy-field-wide deploy-manifest-field">
            <span>{zh ? "环境变量（可选）" : "Environment (.env optional)"}</span>
            <textarea
              value={envText}
              onChange={(event) => setEnvText(event.target.value)}
              placeholder={"DATABASE_URL=postgres://...\nAPI_KEY=..."}
              spellCheck={false}
            />
            <small className="deploy-muted">
              {zh
                ? "写入控制面的 scoped secrets；部署文件或 Compose 中引用 ${DATABASE_URL} 即可。"
                : "Saved as scoped control-plane secrets; reference them as ${DATABASE_URL} in the manifest or Compose file."}
            </small>
          </label>
        </section>

        <section className="deploy-config-section">
          <button type="button" className="ghost registry-setup-toggle" onClick={() => setShowAdvanced((current) => !current)}>
            <Settings2 size={15} aria-hidden="true" />
            {zh ? "Advanced" : "Advanced"}
            <span>{showAdvanced ? "▾" : "▸"}</span>
          </button>
          {showAdvanced ? (
            <div className="deploy-field-grid">
              <label>
                <span>{zh ? "构建平台" : "Build platform"}</span>
                <input type="text" value={platform} placeholder="linux/amd64" onChange={(event) => setPlatform(event.target.value)} />
              </label>
              <label>
                <span>{zh ? "Registry host" : "Registry host"}</span>
                <input type="text" value={registryHost} placeholder="100.66.177.70:5000" onChange={(event) => setRegistryHost(event.target.value)} />
              </label>
              <label>
                <span>{zh ? "Push host" : "Push host"}</span>
                <input type="text" value={pushHost} placeholder="localhost:5000" onChange={(event) => setPushHost(event.target.value)} />
              </label>
              <label>
                <span>{zh ? "Context" : "Context"}</span>
                <input type="text" value={context} placeholder="." onChange={(event) => setContext(event.target.value)} />
              </label>
              <label>
                <span>Dockerfile</span>
                <input type="text" value={dockerfile} placeholder="Dockerfile" onChange={(event) => setDockerfile(event.target.value)} />
              </label>
            </div>
          ) : null}
        </section>

        {errors.length ? (
          <ul className="deploy-muted">
            {errors.map((message) => <li key={message}>{message}</li>)}
          </ul>
        ) : null}
        {error ? <div className="deploy-muted">{error}</div> : null}

        {steps.length ? (
          <ol className="deploy-step-log">
            {steps.filter((step) => step.name).map((step, index) => (
              <li key={`${step.name}-${index}`} className={`step-${step.status || "ok"}`}>
                <strong>{step.name}</strong>
                {step.message ? <span> - {step.message}</span> : null}
              </li>
            ))}
          </ol>
        ) : null}
      </div>

      <section className="deploy-config-section">
        <header><span>04</span><h3>{zh ? "最近构建" : "Recent builds"}</h3></header>
        <div className="credentials-list">
          {buildRuns.length ? buildRuns.slice(0, 8).map((run) => (
            <article key={run.id || run.source} className="credential-row">
              <div>
                <strong>{run.repository || run.source || run.id}</strong>
                <span>{[run.status, run.buildNode, run.ref].filter(Boolean).join(" · ") || "-"}</span>
                {run.message ? <small className="deploy-muted">{run.message}</small> : null}
              </div>
              <div className="credential-actions">
                <button type="button" className="ghost" disabled={runAction === run.id} onClick={() => void openBuildRun(run.id)}>
                  {runAction === run.id ? (zh ? "读取中..." : "Loading...") : (zh ? "日志" : "Logs")}
                </button>
                <button type="button" className="ghost" disabled={runAction === run.id || !run.id} onClick={() => void retryRun(run.id)}>
                  <RotateCcw size={14} aria-hidden="true" />
                  {zh ? "重试" : "Retry"}
                </button>
              </div>
            </article>
          )) : (
            <div className="deploy-muted">{runsLoading ? (zh ? "读取构建任务中..." : "Loading build runs...") : (zh ? "暂无构建任务" : "No build runs yet")}</div>
          )}
        </div>
        {selectedRun ? (
          <ol className="deploy-step-log">
            {(selectedRun.events || []).filter((step) => step.name).map((step, index) => (
              <li key={`${selectedRun.id}-${step.name}-${index}`} className={`step-${step.status || "ok"}`}>
                <strong>{step.name}</strong>
                {step.message ? <span> - {step.message}</span> : null}
              </li>
            ))}
          </ol>
        ) : null}
      </section>

      <div className="deploy-action-bar">
        <div>
          <strong>{mode === "provider" ? providerId || (zh ? "Git provider" : "Git provider") : (zh ? "手填仓库" : "Manual repository")}</strong>
          <span>{mode === "provider" ? repository || (zh ? "选择仓库后即可构建部署" : "Select a repository to build and deploy") : repoUrl || (zh ? "临时仓库 URL" : "Temporary repository URL")}</span>
        </div>
        <button type="button" disabled={status !== "idle" || errors.length > 0 || repositoryLoading || refLoading} onClick={() => void run()}>
          <Rocket size={16} aria-hidden="true" />
          {status === "running" ? (zh ? "构建并部署中..." : "Building and deploying...") : (zh ? "构建并部署" : "Build and deploy")}
        </button>
      </div>
    </>
  );
}

export function GithubImportEntryCard({ lang, onOpen }: { lang: Lang; onOpen: () => void }) {
  const zh = lang === "zh";
  return (
    <button type="button" className="github-import-entry" onClick={onOpen}>
      <GitBranch size={18} aria-hidden="true" />
      <div>
        <strong>{zh ? "仓库导入" : "Repository import"}</strong>
        <span>{zh ? "选择 Git provider 账户和仓库，自动构建并部署" : "Choose a Git provider account and repo, then build and deploy"}</span>
      </div>
      <span className="github-import-entry-action">{zh ? "打开" : "Open"} -&gt;</span>
    </button>
  );
}
