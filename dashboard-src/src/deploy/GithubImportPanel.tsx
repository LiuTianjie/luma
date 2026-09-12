import { AlertCircle, ChevronDown, GitBranch, Rocket, Server, Settings2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  fetchGitProviderRefs,
  fetchGitProviderRepositories,
  fetchGitProviders,
  type GitProviderCredential,
  type GitRef,
  type GitRepository,
} from "../controlResourcesApi";
import { useRouter } from "../router";
import { ROUTE_BY_PAGE } from "../routes";
import type { DashboardBuildNode, DashboardNode, Lang } from "../types";
import { buildImportStream, registryServeStream } from "./deployApi";
import { isReadyNode, regionChoices } from "./options";
import { StepLog } from "./StepLog";
import { SelectControl } from "../components/primitives";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { PageHeader } from "../pages/PageHeader";
import type { DeployStep, Exposure, Region } from "./types";
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
  regions,
  onBack,
  onRefresh,
  onImported,
}: {
  lang: Lang;
  token: string;
  nodes: DashboardNode[];
  regions?: Region[];
  build?: {
    defaultNode?: string;
    registryHost?: string;
    pushHost?: string;
    nodes?: DashboardBuildNode[];
  };
  onBack?: () => void;
  onRefresh: () => Promise<void> | void;
  onImported?: () => void;
}) {
  const zh = lang === "zh";
  const regionOptions = regions && regions.length ? regions : regionChoices([], nodes);
  const router = useRouter();
  const candidates = useMemo(() => buildNodes(nodes, build?.nodes || []), [build?.nodes, nodes]);
  const preferredBuildNode = useMemo(() => {
    const defaultNode = build?.defaultNode || "";
    return candidates.some((node) => node.name === defaultNode) ? defaultNode : candidates[0]?.name || "";
  }, [build?.defaultNode, candidates]);
  const clusterRegistryHost = (build?.registryHost || "").trim();

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

  // Expand only when the cluster has no configured registry yet.
  const [showRegistry, setShowRegistry] = useState(() => !clusterRegistryHost);
  const [registryNode, setRegistryNode] = useState(preferredBuildNode);
  const [registrySteps, setRegistrySteps] = useState<DeployStep[]>([]);
  const [registryStatus, setRegistryStatus] = useState<"idle" | "running">("idle");
  const [registryError, setRegistryError] = useState("");
  const [registryDone, setRegistryDone] = useState("");

  useEffect(() => {
    if (!buildNode && preferredBuildNode) setBuildNode(preferredBuildNode);
    if (!registryNode && preferredBuildNode) setRegistryNode(preferredBuildNode);
  }, [buildNode, preferredBuildNode, registryNode]);

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
      await onRefresh();
      onImported?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setStatus("idle");
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
    <section className="flex flex-col gap-6">
      <PageHeader
        meta={{
          eyebrow: zh ? "交付 / 新建构建" : "Delivery / New build",
          title: zh ? "从 Git 构建并部署" : "Build and deploy from Git",
          description: zh ? "选择代码来源和构建节点。部署配置默认沿用仓库，也可以在下方覆盖。" : "Choose a source and build node. Use the repository’s deployment configuration or override it below.",
          metrics: [
            { label: zh ? "可用构建节点" : "Build nodes", value: candidates.length },
            { label: "Registry", value: clusterRegistryHost || (zh ? "尚未配置" : "Not configured") },
          ],
        }}
      />

      <div className="flex flex-col gap-8">
        <section className="flex flex-col gap-4">
          <h2 className="text-sm font-medium">{zh ? "01  代码来源" : "01  Source repository"}</h2>
          <Tabs value={mode} onValueChange={(value) => { if (value === "provider" || value === "manual") setMode(value); }}>
            <TabsList aria-label={zh ? "仓库来源" : "Repository source"}>
              <TabsTrigger value="provider" disabled={!providers.length && !providerLoading}>
                <GitBranch data-icon="inline-start" />
                {zh ? "已托管凭据" : "Saved providers"}
              </TabsTrigger>
              <TabsTrigger value="manual">{zh ? "手填 URL" : "Repo URL"}</TabsTrigger>
            </TabsList>
          </Tabs>

          {mode === "provider" ? (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <Field>
                <FieldLabel>{zh ? "Git provider" : "Git provider"}</FieldLabel>
                <SelectControl
                  className="min-w-0"
                  value={providerType}
                  onChange={(value) => setProviderType(value as (typeof PROVIDER_TYPES)[number])}
                  options={PROVIDER_TYPES.map((type) => ({ value: type, label: providerTypeLabel(type, lang) }))}
                />
              </Field>
              <Field>
                <FieldLabel>{zh ? "账户凭据" : "Account credential"}</FieldLabel>
                <SelectControl
                  className="min-w-0"
                  value={providerId}
                  onChange={setProviderId}
                  options={[
                    { value: "", label: providerLoading ? (zh ? "读取中..." : "Loading...") : (zh ? "选择账户" : "Select account") },
                    ...accounts.map((provider) => ({ value: provider.id || "", label: providerAccountLabel(provider) })),
                  ]}
                />
                {!accounts.length && !providerLoading ? (
                  <FieldDescription>
                    {zh ? "还没有该 provider 的账户凭据。" : "No account credentials for this provider yet."}
                    {" "}
                    <Button type="button" variant="link" className="h-auto px-0" onClick={() => router.navigate(ROUTE_BY_PAGE.credentials)}>
                      {zh ? "打开凭据设置" : "Open credentials"}
                    </Button>
                  </FieldDescription>
                ) : null}
              </Field>
              <Field className="md:col-span-2">
                <FieldLabel>{zh ? "仓库" : "Repository"}</FieldLabel>
                <SelectControl
                  className="min-w-0"
                  value={repository}
                  disabled={!providerId || repositoryLoading}
                  onChange={setRepository}
                  options={[
                    { value: "", label: repositoryLoading ? (zh ? "读取仓库中..." : "Loading repositories...") : (zh ? "选择仓库" : "Select repository") },
                    ...repositories.map((repo) => ({ value: repo.fullName, label: repoOptionLabel(repo) })),
                  ]}
                />
              </Field>
              <Field>
                <FieldLabel>{zh ? "分支 / Tag" : "Branch / tag"}</FieldLabel>
                <SelectControl
                  className="min-w-0"
                  value={ref}
                  disabled={!repository || refLoading}
                  onChange={setRef}
                  options={[
                    { value: "", label: refLoading ? (zh ? "读取 refs 中..." : "Loading refs...") : (zh ? "默认分支" : "Default branch") },
                    ...refs.map((item) => ({ value: item.name, label: `${item.name} (${item.type})` })),
                  ]}
                />
              </Field>
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <Field className="md:col-span-2">
                <FieldLabel>{zh ? "仓库 URL" : "Repository URL"}</FieldLabel>
                <Input type="text" value={repoUrl} placeholder="https://github.com/owner/repo" onChange={(event) => setRepoUrl(event.target.value)} />
              </Field>
              <Field>
                <FieldLabel>{zh ? "分支 / Tag（可选）" : "Branch / tag (optional)"}</FieldLabel>
                <Input type="text" value={ref} placeholder="main" onChange={(event) => setRef(event.target.value)} />
              </Field>
            </div>
          )}
          {sourceError ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "代码来源读取失败" : "Could not load source"}</AlertTitle><AlertDescription>{sourceError}</AlertDescription></Alert> : null}
        </section>

        <section className="flex flex-col gap-4">
          <h2 className="text-sm font-medium">{zh ? "02  构建目标" : "02  Build target"}</h2>
          <div className="deploy-field-grid deploy-field-grid-single">
            <label>
              <span>{zh ? "构建节点" : "Build node"}</span>
              <SelectControl
                className="min-w-0"
                value={buildNode}
                onChange={setBuildNode}
                options={[
                  { value: "", label: zh ? "选择构建节点" : "Select build node" },
                  ...candidates.map((node) => ({ value: node.name || "", label: node.displayName || node.name || "" })),
                ]}
              />
              {!candidates.length ? <small className="deploy-muted">{zh ? "当前没有可用的声明构建节点，节点需具备 docker-build 能力。" : "No declared builder node is currently available; the node must advertise docker-build."}</small> : null}
            </label>
          </div>
          <div className="registry-setup">
            {clusterRegistryHost && !showRegistry ? (
              <div className="registry-status-strip">
                <Server size={15} aria-hidden="true" />
                <span>
                  <strong>{zh ? "集群 registry" : "Cluster registry"}</strong>
                  <small>{clusterRegistryHost}</small>
                </span>
                <Button type="button" variant="outline" size="sm" onClick={() => setShowRegistry(true)}>
                  {zh ? "管理" : "Manage"}
                </Button>
              </div>
            ) : null}
            <Button variant="outline" type="button"
 className="w-fit"
 aria-expanded={showRegistry}
 onClick={() => setShowRegistry((current) => !current)}
            >
              <Server size={15} aria-hidden="true" />
              {zh ? "内部 registry 设置" : "Internal registry setup"}
              <ChevronDown size={15} className={showRegistry ? "disclosure-chevron open" : "disclosure-chevron"} aria-hidden="true" />
            </Button>
            {showRegistry ? (
              <div className="registry-setup-body form-disclosure-body">
                <div className="registry-setup-row">
                  <label>
                    <span>{zh ? "registry 所在节点" : "Registry node"}</span>
                    <SelectControl
                      className="min-w-0"
                      value={registryNode}
                      onChange={setRegistryNode}
                      options={[
                        { value: "", label: zh ? "选择节点" : "Select node" },
                        ...candidates.map((node) => ({ value: node.name || "", label: node.displayName || node.name || "" })),
                      ]}
                    />
                    {!candidates.length ? <small className="deploy-muted">{zh ? "内部 registry 需要部署到已声明且可用的构建节点。" : "Internal registry setup needs a declared, available builder node."}</small> : null}
                  </label>
                  <Button type="button"

 disabled={registryStatus !== "idle" || !registryNode}
 onClick={() => void runRegistry()}
                  >
                    <Server size={15} aria-hidden="true" />
                    {registryStatus === "running" ? (zh ? "部署中..." : "Deploying...") : (zh ? "部署 registry" : "Deploy registry")}
                  </Button>
                </div>
                {registryDone ? <small className="deploy-muted registry-done">{registryDone}</small> : null}
                {registryError ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "Registry 部署失败" : "Registry deploy failed"}</AlertTitle><AlertDescription>{registryError}</AlertDescription></Alert> : null}
                {registrySteps.length ? <StepLog steps={registrySteps} lang={lang} /> : null}
              </div>
            ) : null}
          </div>
        </section>

        <section className="flex flex-col gap-4">
          <h2 className="text-sm font-medium">{zh ? "03  部署覆盖项" : "03  Deploy overrides"}</h2>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <label>
              <span>{zh ? "区域" : "Region"}</span>
              <SelectControl
                className="min-w-0"
                value={region}
                onChange={(value) => setRegion(value as Region | "")}
                options={[
                  { value: "", label: zh ? "不覆盖（跟随仓库）" : "No override (use repo)" },
                  ...regionOptions.map((value) => ({ value, label: value })),
                ]}
              />
            </label>
            <label>
              <span>{zh ? "入口模式" : "Exposure"}</span>
              <SelectControl
                className="min-w-0"
                value={exposure}
                onChange={(value) => setExposure(value as Exposure | "")}
                options={[
                  { value: "", label: zh ? "不覆盖（跟随仓库）" : "No override (use repo)" },
                  ...EXPOSURES.map((value) => ({ value, label: value })),
                ]}
              />
            </label>
            <label>
              <span>{zh ? "域名" : "Domain"}</span>
              <Input type="text" value={domain} placeholder="app.example.com" onChange={(event) => setDomain(event.target.value)} />
            </label>
            <label>
              <span>{zh ? "端口" : "Port"}</span>
              <Input type="text" value={port} placeholder="8080" onChange={(event) => setPort(event.target.value)} />
            </label>
          </div>
          <label className="deploy-field-wide deploy-manifest-field">
            <span>{zh ? "Luma 部署文件（可选）" : "Luma manifest (optional)"}</span>
            <Textarea
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
            <Textarea
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
          <Button variant="outline" type="button"
 className="w-fit"
 aria-expanded={showAdvanced}
 onClick={() => setShowAdvanced((current) => !current)}
          >
            <Settings2 size={15} aria-hidden="true" />
            {zh ? "高级选项" : "Advanced"}
            <ChevronDown size={15} className={showAdvanced ? "disclosure-chevron open" : "disclosure-chevron"} aria-hidden="true" />
          </Button>
          {showAdvanced ? (
            <div className="form-disclosure-body">
              <div className="deploy-field-grid">
                <label>
                  <span>{zh ? "构建平台" : "Build platform"}</span>
                  <Input type="text" value={platform} placeholder="linux/amd64" onChange={(event) => setPlatform(event.target.value)} />
                </label>
                <label>
                  <span>{zh ? "Registry 地址" : "Registry host"}</span>
                  <Input type="text" value={registryHost} placeholder="100.66.177.70:5000" onChange={(event) => setRegistryHost(event.target.value)} />
                </label>
                <label>
                  <span>{zh ? "Push 地址" : "Push host"}</span>
                  <Input type="text" value={pushHost} placeholder="localhost:5000" onChange={(event) => setPushHost(event.target.value)} />
                </label>
                <label>
                  <span>{zh ? "构建上下文" : "Context"}</span>
                  <Input type="text" value={context} placeholder="." onChange={(event) => setContext(event.target.value)} />
                </label>
                <label>
                  <span>Dockerfile</span>
                  <Input type="text" value={dockerfile} placeholder="Dockerfile" onChange={(event) => setDockerfile(event.target.value)} />
                </label>
              </div>
            </div>
          ) : null}
        </section>

        {errors.length ? (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>{zh ? "还不能开始构建" : "Cannot start build"}</AlertTitle>
            <AlertDescription>
              <ul className="list-disc pl-4">
                {errors.map((message) => <li key={message}>{message}</li>)}
              </ul>
            </AlertDescription>
          </Alert>
        ) : null}
        {error ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "构建失败" : "Build failed"}</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}

        {steps.length ? <><StepLog steps={steps} lang={lang} /><Button variant="outline" type="button" onClick={() => { const id = steps.find((step) => step.buildRunId)?.buildRunId; router.navigate(id ? `/deployments/build/${encodeURIComponent(id)}` : "/deployments?kind=build"); }}>{zh ? "查看持久构建记录" : "View build record"}</Button></> : null}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
        <div>
          <strong>{mode === "provider" ? providerId || (zh ? "Git provider" : "Git provider") : (zh ? "手填仓库" : "Manual repository")}</strong>
          <span>{mode === "provider" ? repository || (zh ? "选择仓库后即可构建部署" : "Select a repository to build and deploy") : repoUrl || (zh ? "临时仓库 URL" : "Temporary repository URL")}</span>
        </div>
        <Button type="button" disabled={status !== "idle" || errors.length> 0 || repositoryLoading || refLoading} onClick={() => void run()}>
          <Rocket size={16} aria-hidden="true" />
          {status === "running" ? (zh ? "构建并部署中..." : "Building and deploying...") : (zh ? "构建并部署" : "Build and deploy")}
        </Button>
      </div>
    </section>
  );
}

export function GithubImportEntryCard({ lang, onOpen }: { lang: Lang; onOpen: () => void }) {
  const zh = lang === "zh";
  return (
    <Card
      className="template-repository-entry cursor-pointer transition-colors hover:bg-muted/50"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen();
        }
      }}
    >
      <CardContent className="flex items-center gap-4 py-4">
        <GitBranch size={18} className="text-primary" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">{zh ? "仓库导入" : "Repository import"}</p>
          <p className="text-sm text-muted-foreground">
            {zh ? "从已连接的 Git 仓库构建并部署应用" : "Build and deploy an application from a connected Git repository"}
          </p>
        </div>
        <span className="text-sm text-muted-foreground">{zh ? "打开" : "Open"} →</span>
      </CardContent>
    </Card>
  );
}
