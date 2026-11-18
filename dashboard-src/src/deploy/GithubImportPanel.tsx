import { AlertCircle, ArrowLeft, ArrowRight, ChevronDown, ChevronUp, GitBranch, Rocket, Server, Settings2 } from "@/components/icons";
import { useEffect, useMemo, useRef, useState } from "react";
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
import type { DashboardNode, DashboardRegion, Lang } from "../types";
import { buildImportStream, fetchBuilderStatus, type BuilderStatus } from "./deployApi";
import { EXPOSURES, exposureAllowedInRegion, regionChoices } from "./options";
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
const PROVIDER_TYPES = ["github", "gitea"] as const;

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
  regions,
  regionCatalog,
  onBack,
  onRefresh,
  onImported,
}: {
  lang: Lang;
  token: string;
  nodes: DashboardNode[];
  regions?: Region[];
  regionCatalog?: DashboardRegion[];
  onBack?: () => void;
  onRefresh: () => Promise<void> | void;
  onImported?: () => void;
}) {
  const zh = lang === "zh";
  const regionOptions = regions && regions.length ? regions : regionChoices(regionCatalog, nodes);
  const router = useRouter();
  const [builderStatus, setBuilderStatus] = useState<BuilderStatus | null>(null);
  const [builderError, setBuilderError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    fetchBuilderStatus(token, controller.signal).then(setBuilderStatus).catch((error) => {
      if (!controller.signal.aborted) setBuilderError(error instanceof Error ? error.message : String(error));
    });
    return () => controller.abort();
  }, [token]);

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

  const [ref, setRef] = useState("");
  const [region, setRegion] = useState<Region | "">("");
  const [exposure, setExposure] = useState<Exposure | "">("");
  const [domain, setDomain] = useState("");
  const [port, setPort] = useState("");
  const [manifest, setManifest] = useState("");
  const [envText, setEnvText] = useState("");
  const [platform, setPlatform] = useState("");
  const [context, setContext] = useState("");
  const [dockerfile, setDockerfile] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const followController = useRef<AbortController | null>(null);
  useEffect(() => () => followController.current?.abort(), []);
  const [steps, setSteps] = useState<DeployStep[]>([]);
  const [status, setStatus] = useState<"idle" | "running">("idle");
  const [error, setError] = useState("");

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
    if (exposure && exposure !== "none" && !domain.trim()) list.push(zh ? "公开入口必须填写域名" : "Public exposure requires a domain");
    if (region && exposure && !exposureAllowedInRegion(exposure, region, regionCatalog)) list.push(zh ? `区域 ${region} 尚未启用 ${exposure}` : `Region ${region} has not enabled ${exposure}`);
    if (port.trim() && !/^[0-9]+$/.test(port.trim())) list.push(zh ? "端口必须是正整数" : "Port must be a positive integer");
    for (const message of parsedEnv.errors) list.push(zh ? `环境变量 ${message}` : `Environment ${message}`);
    return list;
  }, [domain, envText, exposure, mode, port, providerId, region, regionCatalog, repoUrl, repository, zh]);

  const run = async () => {
    if (errors.length) return;
    const parsedEnv = parseEnvText(envText);
    const controller = new AbortController();
    followController.current = controller;
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
          ref: ref.trim(),
          region: region || undefined,
          exposure: exposure || undefined,
          domain: domain.trim(),
          port: port.trim(),
          manifest: manifest.trim(),
          platform: platform.trim(),
          context: context.trim(),
          dockerfile: dockerfile.trim(),
          envSecrets: Object.keys(parsedEnv.values).length ? parsedEnv.values : undefined,
        },
        (step) => setSteps((current) => [...current, step]),
        controller.signal,
      );
      await onRefresh();
      onImported?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setStatus("idle");
    }
  };

  return <section className="flex min-w-0 flex-col gap-6">
    {onBack ? <Button type="button" variant="outline" className="w-fit" onClick={onBack} disabled={status !== "idle"}><ArrowLeft data-icon="inline-start" />{zh ? "返回模板" : "Back to templates"}</Button> : null}
    <PageHeader meta={{
      eyebrow: zh ? "应用 / 从 Git 构建" : "Applications / Build from Git",
      title: zh ? "从 Git 构建并部署" : "Build and deploy from Git",
      description: zh ? "选择代码来源并提交，空闲 Runner 会按目标架构自动领取构建任务，镜像使用 Builder 配置的镜像仓库。" : "Choose a source and submit. An idle Runner compatible with the target architecture claims the build, using the registry configured in Builder.",
      metrics: [],
    }} />
    <DeployFormSection title={zh ? "01 代码来源" : "01 Source repository"}>
      <FieldGroup>
        <Field>
          <FieldLabel id="git-source-mode">{zh ? "仓库来源" : "Repository source"}</FieldLabel>
          <ToggleGroup value={[mode]} variant="segmented" aria-labelledby="git-source-mode" onValueChange={(values) => { if (values[0] === "provider" || values[0] === "manual") setMode(values[0]); }}>
            <ToggleGroupItem value="provider" disabled={!providers.length && !providerLoading}><GitBranch data-icon="inline-start" />{zh ? "已托管凭据" : "Saved providers"}</ToggleGroupItem>
            <ToggleGroupItem value="manual">{zh ? "手填 URL" : "Repo URL"}</ToggleGroupItem>
          </ToggleGroup>
        </Field>
        {mode === "provider" ? <FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
          <DeploySelectField label={zh ? "代码托管服务" : "Git provider"} value={providerType} onChange={(value) => setProviderType(value as (typeof PROVIDER_TYPES)[number])} options={PROVIDER_TYPES.map((type) => ({ value: type, label: providerTypeLabel(type, lang) }))} />
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
    <Alert><Server /><AlertTitle>{zh ? "自动调度构建" : "Automatic build scheduling"}</AlertTitle><AlertDescription>
      {zh ? "无需选择构建节点或 Registry。任务进入 Builder 队列，匹配架构的空闲 Runner 自动领取；全部忙碌时继续排队。" : "No build node or registry selection is needed. A compatible idle Runner claims the task from Builder; tasks wait in the queue while Runners are busy."}
      {builderStatus?.registry?.registryHost ? <p>{zh ? "镜像仓库：" : "Registry: "}<code>{builderStatus.registry.registryHost}{builderStatus.registry.namespace ? `/${builderStatus.registry.namespace}` : ""}</code></p> : null}
    </AlertDescription></Alert>
    {builderError ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "Builder 暂时无法读取" : "Builder status unavailable"}</AlertTitle><AlertDescription>{builderError}</AlertDescription></Alert> : null}
    <DeployFormSection title={zh ? "02 部署覆盖项" : "02 Deploy overrides"}>
      <FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
        <DeploySelectField label={zh ? "区域" : "Region"} value={region} onChange={(value) => setRegion(value as Region | "")} options={[
          { value: "", label: zh ? "不覆盖（跟随仓库）" : "No override (use repo)" },
          ...regionOptions.map((value) => ({ value, label: value })),
        ]} />
        <DeploySelectField label={zh ? "入口模式" : "Exposure"} value={exposure} onChange={(value) => setExposure(value as Exposure | "")} options={[
          { value: "", label: zh ? "不覆盖（跟随仓库）" : "No override (use repo)" },
          ...EXPOSURES.map((value) => ({ value, label: value, disabled: Boolean(region && !exposureAllowedInRegion(value, region, regionCatalog)) })),
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
          <CardDescription>{zh ? "按需覆盖构建平台和 Dockerfile 路径。" : "Optionally override the build platform and Dockerfile paths."}</CardDescription>
        </CardHeader>
        <CollapsibleContent><CardContent className="@container"><FieldGroup className="grid grid-cols-1 items-start gap-4 @md:grid-cols-2">
          <DeployTextField label={zh ? "构建平台" : "Build platform"} type="text" value={platform} placeholder="linux/amd64" onChange={(event) => setPlatform(event.target.value)} />
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
      <CardFooter className="justify-end"><Button variant="outline" type="button" onClick={() => router.navigate("/deployments")} >{zh ? "查看部署记录" : "View deployments"}<ArrowRight data-icon="inline-end" /></Button></CardFooter>
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
