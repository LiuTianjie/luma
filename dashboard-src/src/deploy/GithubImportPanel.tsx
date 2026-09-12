import { AlertCircle, ArrowLeft, ArrowRight, CheckCircle2, ChevronDown, ChevronUp, GitBranch, Rocket, Server, Settings2 } from "lucide-react";
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
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Spinner } from "@/components/ui/spinner";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { DeployFormSection, DeploySelectField, DeployTextareaField, DeployTextField } from "./DeployFormFields";
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

  return <section className="flex min-w-0 flex-col gap-6">
    {onBack ? <Button type="button" variant="outline" className="w-fit" onClick={onBack} disabled={status !== "idle"}><ArrowLeft data-icon="inline-start" />{zh ? "返回模板" : "Back to templates"}</Button> : null}
    <PageHeader meta={{
      eyebrow: zh ? "交付 / 新建构建" : "Delivery / New build",
      title: zh ? "从 Git 构建并部署" : "Build and deploy from Git",
      description: zh ? "选择代码来源和构建节点。部署配置默认沿用仓库，也可以在下方覆盖。" : "Choose a source and build node. Use the repository’s deployment configuration or override it below.",
      metrics: [{ label: zh ? "可用构建节点" : "Build nodes", value: candidates.length }, { label: "Registry", value: clusterRegistryHost || (zh ? "尚未配置" : "Not configured") }],
    }} />
    <DeployFormSection title={zh ? "01 代码来源" : "01 Source repository"}>
      <FieldGroup>
        <Field>
          <FieldLabel id="git-source-mode">{zh ? "仓库来源" : "Repository source"}</FieldLabel>
          <ToggleGroup value={[mode]} variant="outline" aria-labelledby="git-source-mode" onValueChange={(values) => { if (values[0] === "provider" || values[0] === "manual") setMode(values[0]); }} className="max-w-full flex-wrap">
            <ToggleGroupItem value="provider" disabled={!providers.length && !providerLoading}><GitBranch data-icon="inline-start" />{zh ? "已托管凭据" : "Saved providers"}</ToggleGroupItem>
            <ToggleGroupItem value="manual">{zh ? "手填 URL" : "Repo URL"}</ToggleGroupItem>
          </ToggleGroup>
        </Field>
        {mode === "provider" ? <FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
          <DeploySelectField label="Git provider" value={providerType} onChange={(value) => setProviderType(value as (typeof PROVIDER_TYPES)[number])} options={PROVIDER_TYPES.map((type) => ({ value: type, label: providerTypeLabel(type, lang) }))} />
          <DeploySelectField label={zh ? "账户凭据" : "Account credential"} value={providerId} onChange={setProviderId} disabled={providerLoading} options={[
            { value: "", label: providerLoading ? (zh ? "读取中…" : "Loading…") : (zh ? "选择账户" : "Select account") },
            ...accounts.map((provider) => ({ value: provider.id || "", label: providerAccountLabel(provider) })),
          ]} description={!accounts.length && !providerLoading ? (zh ? "还没有该 provider 的账户凭据。" : "No account credentials for this provider yet.") : undefined} />
          {!accounts.length && !providerLoading ? <Button type="button" variant="outline" className="w-fit @md:col-span-2" onClick={() => router.navigate(ROUTE_BY_PAGE.credentials)}>{zh ? "打开凭据设置" : "Open credentials"}<ArrowRight data-icon="inline-end" /></Button> : null}
          <DeploySelectField wide label={zh ? "仓库" : "Repository"} value={repository} disabled={!providerId || repositoryLoading} onChange={setRepository} options={[
            { value: "", label: repositoryLoading ? (zh ? "读取仓库中…" : "Loading repositories…") : (zh ? "选择仓库" : "Select repository") },
            ...repositories.map((repo) => ({ value: repo.fullName, label: repoOptionLabel(repo) })),
          ]} />
          <DeploySelectField label={zh ? "分支 / Tag" : "Branch / tag"} value={ref} disabled={!repository || refLoading} onChange={setRef} options={[
            { value: "", label: refLoading ? (zh ? "读取 refs 中…" : "Loading refs…") : (zh ? "默认分支" : "Default branch") },
            ...refs.map((item) => ({ value: item.name, label: `${item.name} (${item.type})` })),
          ]} />
        </FieldGroup> : <FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
          <DeployTextField wide label={zh ? "仓库 URL" : "Repository URL"} type="text" value={repoUrl} placeholder="https://github.com/owner/repo" onChange={(event) => setRepoUrl(event.target.value)} error={!repoUrl.trim() ? (zh ? "请输入仓库 URL" : "Enter a repository URL") : undefined} />
          <DeployTextField label={zh ? "分支 / Tag（可选）" : "Branch / tag (optional)"} type="text" value={ref} placeholder="main" onChange={(event) => setRef(event.target.value)} />
        </FieldGroup>}
      </FieldGroup>
      {sourceError ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "代码来源读取失败" : "Could not load source"}</AlertTitle><AlertDescription>{sourceError}</AlertDescription></Alert> : null}
    </DeployFormSection>
    <DeployFormSection title={zh ? "02 构建目标" : "02 Build target"}>
      <FieldGroup><DeploySelectField label={zh ? "构建节点" : "Build node"} value={buildNode} onChange={setBuildNode} options={[
        { value: "", label: zh ? "选择构建节点" : "Select build node" },
        ...candidates.map((node) => ({ value: node.name || "", label: node.displayName || node.name || "" })),
      ]} description={!candidates.length ? (zh ? "当前没有可用的声明构建节点，节点需具备 docker-build 能力。" : "No declared builder node is currently available; the node must advertise docker-build.") : undefined} error={!buildNode ? (zh ? "请选择构建节点" : "Select a build node") : undefined} /></FieldGroup>
      {clusterRegistryHost ? <Alert><Server /><AlertTitle>{zh ? "集群 Registry" : "Cluster registry"}</AlertTitle><AlertDescription>{clusterRegistryHost}</AlertDescription></Alert> : null}
      <Collapsible open={showRegistry} onOpenChange={setShowRegistry} className="flex flex-col gap-4">
        <CollapsibleTrigger render={<Button variant="outline" className="w-fit" />}>
          <Server data-icon="inline-start" />{zh ? "内部 Registry 设置" : "Internal registry setup"}{showRegistry ? <ChevronUp data-icon="inline-end" /> : <ChevronDown data-icon="inline-end" />}
        </CollapsibleTrigger>
        <CollapsibleContent className="flex flex-col gap-4">
          <FieldGroup><DeploySelectField label={zh ? "Registry 所在节点" : "Registry node"} value={registryNode} onChange={setRegistryNode} disabled={registryStatus !== "idle"} options={[
            { value: "", label: zh ? "选择节点" : "Select node" },
            ...candidates.map((node) => ({ value: node.name || "", label: node.displayName || node.name || "" })),
          ]} description={!candidates.length ? (zh ? "内部 Registry 需要部署到已声明且可用的构建节点。" : "Internal registry setup needs a declared, available builder node.") : undefined} /></FieldGroup>
          <Button type="button" className="w-fit" disabled={registryStatus !== "idle" || !registryNode} onClick={() => void runRegistry()}>
            {registryStatus === "running" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <Server data-icon="inline-start" />}{registryStatus === "running" ? (zh ? "部署中…" : "Deploying…") : (zh ? "部署 Registry" : "Deploy registry")}
          </Button>
          {registryDone ? <Alert><CheckCircle2 /><AlertTitle>{zh ? "Registry 部署完成" : "Registry deployed"}</AlertTitle><AlertDescription>{registryDone}</AlertDescription></Alert> : null}
          {registryError ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "Registry 部署失败" : "Registry deploy failed"}</AlertTitle><AlertDescription>{registryError}</AlertDescription></Alert> : null}
          {registrySteps.length ? <StepLog steps={registrySteps} lang={lang} /> : null}
        </CollapsibleContent>
      </Collapsible>
    </DeployFormSection>
    <DeployFormSection title={zh ? "03 部署覆盖项" : "03 Deploy overrides"}>
      <FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
        <DeploySelectField label={zh ? "区域" : "Region"} value={region} onChange={(value) => setRegion(value as Region | "")} options={[
          { value: "", label: zh ? "不覆盖（跟随仓库）" : "No override (use repo)" },
          ...regionOptions.map((value) => ({ value, label: value })),
        ]} />
        <DeploySelectField label={zh ? "入口模式" : "Exposure"} value={exposure} onChange={(value) => setExposure(value as Exposure | "")} options={[
          { value: "", label: zh ? "不覆盖（跟随仓库）" : "No override (use repo)" },
          ...EXPOSURES.map((value) => ({ value, label: value })),
        ]} />
        <DeployTextField label={zh ? "域名" : "Domain"} type="text" value={domain} placeholder="app.example.com" onChange={(event) => setDomain(event.target.value)} error={exposure && exposure !== "none" && !domain.trim() ? (zh ? "请输入域名" : "Enter a domain") : undefined} />
        <DeployTextField label={zh ? "端口" : "Port"} type="text" inputMode="numeric" value={port} placeholder="8080" onChange={(event) => setPort(event.target.value)} error={port.trim() && !/^[0-9]+$/.test(port.trim()) ? (zh ? "端口必须是正整数" : "Port must be a positive integer") : undefined} />
        <DeployTextareaField wide label={zh ? "Luma 部署文件（可选）" : "Luma manifest (optional)"} value={manifest} onChange={(event) => setManifest(event.target.value)} placeholder={"name: app\nimage: placeholder\nregion: cn\nexposure: none"} spellCheck={false} rows={6} description={zh ? "仓库里有 Luma service 或 Compose 部署文件时会自动使用；这里填写后可作为没有部署文件时的手动输入。" : "If the repository has a Luma service or Compose deployment file, Luma uses it automatically; fill this when the repo has no manifest yet."} />
        <DeployTextareaField wide label={zh ? "环境变量（可选）" : "Environment (.env optional)"} value={envText} onChange={(event) => setEnvText(event.target.value)} placeholder={"DATABASE_URL=postgres://...\nAPI_KEY=..."} spellCheck={false} rows={6} error={parseEnvText(envText).errors.join("; ") || undefined} description={zh ? "写入控制面的 scoped secrets；部署文件或 Compose 中引用 ${DATABASE_URL} 即可。" : "Saved as scoped control-plane secrets; reference them as ${DATABASE_URL} in the manifest or Compose file."} />
      </FieldGroup>
    </DeployFormSection>
    <Collapsible open={showAdvanced} onOpenChange={setShowAdvanced}>
      <Card>
        <CardHeader>
          <CardTitle>{zh ? "高级选项" : "Advanced options"}</CardTitle>
          <CardDescription>{zh ? "按需覆盖构建平台、镜像仓库和 Dockerfile 路径。" : "Optionally override the build platform, registry and Dockerfile paths."}</CardDescription>
        </CardHeader>
        <CollapsibleContent><CardContent className="@container"><FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
          <DeployTextField label={zh ? "构建平台" : "Build platform"} type="text" value={platform} placeholder="linux/amd64" onChange={(event) => setPlatform(event.target.value)} />
          <DeployTextField label={zh ? "Registry 地址" : "Registry host"} type="text" value={registryHost} placeholder="100.66.177.70:5000" onChange={(event) => setRegistryHost(event.target.value)} />
          <DeployTextField label={zh ? "Push 地址" : "Push host"} type="text" value={pushHost} placeholder="localhost:5000" onChange={(event) => setPushHost(event.target.value)} />
          <DeployTextField label={zh ? "构建上下文" : "Context"} type="text" value={context} placeholder="." onChange={(event) => setContext(event.target.value)} />
          <DeployTextField label="Dockerfile" type="text" value={dockerfile} placeholder="Dockerfile" onChange={(event) => setDockerfile(event.target.value)} />
        </FieldGroup></CardContent></CollapsibleContent>
        <CardFooter><CollapsibleTrigger render={<Button variant="outline" />}><Settings2 data-icon="inline-start" />{showAdvanced ? (zh ? "收起高级选项" : "Hide advanced options") : (zh ? "展开高级选项" : "Show advanced options")}{showAdvanced ? <ChevronUp data-icon="inline-end" /> : <ChevronDown data-icon="inline-end" />}</CollapsibleTrigger></CardFooter>
      </Card>
    </Collapsible>
    {errors.length ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "还不能开始构建" : "Cannot start build"}</AlertTitle><AlertDescription><ul className="flex list-disc flex-col gap-2 pl-4">{errors.map((message) => <li key={message}>{message}</li>)}</ul></AlertDescription></Alert> : null}
    {error ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "构建失败" : "Build failed"}</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}
    {steps.length ? <Card className="min-w-0">
      <CardHeader><CardTitle>{zh ? "构建进度" : "Build progress"}</CardTitle></CardHeader>
      <CardContent><StepLog steps={steps} lang={lang} /></CardContent>
      <CardFooter className="justify-end"><Button variant="outline" type="button" onClick={() => { const id = steps.find((step) => step.buildRunId)?.buildRunId; router.navigate(id ? `/deployments/build/${encodeURIComponent(id)}` : "/deployments?kind=build"); }}>{zh ? "查看持久构建记录" : "View build record"}<ArrowRight data-icon="inline-end" /></Button></CardFooter>
    </Card> : null}
    <Card>
      <CardHeader><CardTitle>{mode === "provider" ? providerId || "Git provider" : (zh ? "手填仓库" : "Manual repository")}</CardTitle><CardDescription className="break-words">{mode === "provider" ? repository || (zh ? "选择仓库后即可构建部署" : "Select a repository to build and deploy") : repoUrl || (zh ? "临时仓库 URL" : "Temporary repository URL")}</CardDescription></CardHeader>
      <CardFooter className="justify-end"><Button type="button" disabled={status !== "idle" || errors.length > 0 || repositoryLoading || refLoading} onClick={() => void run()}>
        {status === "running" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <Rocket data-icon="inline-start" />}{status === "running" ? (zh ? "构建并部署中…" : "Building and deploying…") : (zh ? "构建并部署" : "Build and deploy")}
      </Button></CardFooter>
    </Card>
  </section>;
}

export function GithubImportEntryCard({ lang, onOpen }: { lang: Lang; onOpen: () => void }) {
  const zh = lang === "zh";
  return <Card>
    <CardHeader><CardTitle>{zh ? "仓库导入" : "Repository import"}</CardTitle><CardDescription>{zh ? "从已连接的 Git 仓库构建并部署应用" : "Build and deploy an application from a connected Git repository"}</CardDescription></CardHeader>
    <CardFooter><Button variant="outline" onClick={onOpen}><GitBranch data-icon="inline-start" />{zh ? "打开仓库导入" : "Open repository import"}<ArrowRight data-icon="inline-end" /></Button></CardFooter>
  </Card>;
}
