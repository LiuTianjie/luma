import { ArrowLeft, GitBranch, Rocket, Server } from "lucide-react";
import { useMemo, useState } from "react";
import type { DashboardNode, Lang } from "../types";
import { buildImportStream, registryServeStream } from "./deployApi";
import { isReadyNode } from "./options";
import type { DeployStep, Exposure, Region } from "./types";

const REGIONS: Region[] = ["cn", "global", "home"];
const EXPOSURES: Exposure[] = ["none", "cn-edge", "external-edge", "tailscale-relay", "cloudflare-tunnel", "tcp-relay"];

function buildNodes(nodes: DashboardNode[]): DashboardNode[] {
  return nodes.filter((node) => node.name && isReadyNode(node) && (node.storageCapabilities || []).includes("docker-build"));
}

export function GithubImportPanel({
  lang,
  token,
  nodes,
  onBack,
  onRefresh,
}: {
  lang: Lang;
  token: string;
  nodes: DashboardNode[];
  onBack?: () => void;
  onRefresh: () => Promise<void> | void;
}) {
  const zh = lang === "zh";
  const candidates = useMemo(() => buildNodes(nodes), [nodes]);
  const [repoUrl, setRepoUrl] = useState("");
  const [buildNode, setBuildNode] = useState(candidates[0]?.name || "");
  const [ref, setRef] = useState("");
  const [region, setRegion] = useState<Region | "">("");
  const [exposure, setExposure] = useState<Exposure | "">("");
  const [domain, setDomain] = useState("");
  const [port, setPort] = useState("");
  const [platform, setPlatform] = useState("");
  const [steps, setSteps] = useState<DeployStep[]>([]);
  const [status, setStatus] = useState<"idle" | "running">("idle");
  const [error, setError] = useState("");
  // Registry-serve sub-flow: one-time setup of the in-cluster registry.
  const readyNodes = useMemo(() => nodes.filter((node) => node.name && isReadyNode(node)), [nodes]);
  const [showRegistry, setShowRegistry] = useState(false);
  const [registryNode, setRegistryNode] = useState("");
  const [registrySteps, setRegistrySteps] = useState<DeployStep[]>([]);
  const [registryStatus, setRegistryStatus] = useState<"idle" | "running">("idle");
  const [registryError, setRegistryError] = useState("");
  const [registryDone, setRegistryDone] = useState("");

  const errors = useMemo(() => {
    const list: string[] = [];
    if (!repoUrl.trim()) list.push(zh ? "仓库地址不能为空" : "Repository URL is required");
    if (!buildNode) list.push(zh ? "必须选择一个构建节点（需具备 docker-build 能力）" : "Select a build node (must have docker-build capability)");
    if (exposure && exposure !== "none" && !domain.trim()) list.push(zh ? "公开入口必须填写域名" : "Public exposure requires a domain");
    if (port.trim() && !/^[0-9]+$/.test(port.trim())) list.push(zh ? "端口必须是正整数" : "Port must be a positive integer");
    return list;
  }, [repoUrl, buildNode, exposure, domain, port, zh]);

  const run = async () => {
    if (errors.length) return;
    setStatus("running");
    setSteps([]);
    setError("");
    try {
      await buildImportStream(
        {
          token,
          repoUrl: repoUrl.trim(),
          buildNode,
          ref: ref.trim(),
          region: region || undefined,
          exposure: exposure || undefined,
          domain: domain.trim(),
          port: port.trim(),
          platform: platform.trim(),
        },
        (step) => setSteps((current) => [...current, step]),
      );
      await onRefresh();
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
    <>
      <div className="panel-heading deploy-heading">
        <div>
          <p className="eyebrow">{zh ? "从 GitHub 导入" : "Import from GitHub"}</p>
          <h2>{zh ? "构建并部署一个仓库" : "Build and deploy a repository"}</h2>
          <small className="deploy-context-label">
            {zh
              ? "在构建节点上 clone 仓库、按 Dockerfile 构建镜像、推送到集群内 registry，然后部署。仓库需包含 Dockerfile 与 .luma.yml。"
              : "Clone on a build node, build the Dockerfile, push to the in-cluster registry, then deploy. The repo needs a Dockerfile and a .luma.yml."}
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
          <header><span>01</span><h3>{zh ? "仓库与构建节点" : "Repository and build node"}</h3></header>
          <div className="deploy-field-grid">
            <label className="deploy-field-wide">
              <span>{zh ? "GitHub 仓库地址" : "GitHub repository URL"}</span>
              <input type="text" value={repoUrl} placeholder="https://github.com/owner/repo" onChange={(event) => setRepoUrl(event.target.value)} />
            </label>
            <label>
              <span>{zh ? "构建节点" : "Build node"}</span>
              <select value={buildNode} onChange={(event) => setBuildNode(event.target.value)}>
                <option value="">{zh ? "选择构建节点" : "Select build node"}</option>
                {candidates.map((node) => (
                  <option key={node.name} value={node.name}>{node.displayName || node.name}</option>
                ))}
              </select>
              {!candidates.length ? (
                <small className="deploy-muted">
                  {zh ? "当前没有具备 docker-build 能力的就绪节点。请先在目标节点安装 docker buildx。" : "No ready node advertises docker-build. Install docker buildx on the target node first."}
                </small>
              ) : (
                <small className="deploy-muted">{zh ? "需具备 docker-build 能力（已安装 buildx）。" : "Must advertise docker-build (buildx installed)."}</small>
              )}
            </label>
            <label>
              <span>{zh ? "分支 / Tag（可选）" : "Branch / tag (optional)"}</span>
              <input type="text" value={ref} placeholder="main" onChange={(event) => setRef(event.target.value)} />
            </label>
          </div>
          <div className="registry-setup">
            <button type="button" className="ghost registry-setup-toggle" onClick={() => setShowRegistry((current) => !current)}>
              <Server size={15} aria-hidden="true" />
              {zh ? "集群内 registry 设置（一次性）" : "In-cluster registry setup (one-time)"}
              <span>{showRegistry ? "▾" : "▸"}</span>
            </button>
            {showRegistry ? (
              <div className="registry-setup-body">
                <small className="deploy-muted">
                  {zh
                    ? "构建出的镜像需要推送到集群内 registry。若尚未部署，选一个构建节点一键部署，并自动为各节点配置内网拉取。"
                    : "Built images need an in-cluster registry to push to. If not deployed yet, pick a build node to deploy one and auto-configure pulls across nodes."}
                </small>
                <div className="deploy-field-grid">
                  <label>
                    <span>{zh ? "registry 所在节点" : "Registry node"}</span>
                    <select value={registryNode} onChange={(event) => setRegistryNode(event.target.value)}>
                      <option value="">{zh ? "选择节点" : "Select node"}</option>
                      {readyNodes.map((node) => (
                        <option key={node.name} value={node.name}>{node.displayName || node.name}</option>
                      ))}
                    </select>
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
                        {step.message ? <span> — {step.message}</span> : null}
                      </li>
                    ))}
                  </ol>
                ) : null}
              </div>
            ) : null}
          </div>
        </section>

        <section className="deploy-config-section">
          <header><span>02</span><h3>{zh ? "覆盖 .luma.yml（全部可选）" : "Override .luma.yml (all optional)"}</h3></header>
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
            <label>
              <span>{zh ? "构建平台" : "Build platform"}</span>
              <input type="text" value={platform} placeholder="linux/amd64" onChange={(event) => setPlatform(event.target.value)} />
            </label>
          </div>
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
                {step.message ? <span> — {step.message}</span> : null}
              </li>
            ))}
          </ol>
        ) : null}
      </div>

      <div className="deploy-action-bar">
        <div>
          <strong>GITHUB_TOKEN</strong>
          <span>{zh ? "私有仓库需先在 Luma Control 设置 GITHUB_TOKEN 密钥。" : "For private repos, set the GITHUB_TOKEN secret in Luma Control first."}</span>
        </div>
        <button type="button" disabled={status !== "idle" || errors.length > 0} onClick={() => void run()}>
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
        <strong>{zh ? "从 GitHub 导入" : "Import from GitHub"}</strong>
        <span>{zh ? "提供仓库地址，自动构建 Dockerfile 并部署" : "Point at a repo, build its Dockerfile, and deploy"}</span>
      </div>
      <span className="github-import-entry-action">{zh ? "打开" : "Open"} →</span>
    </button>
  );
}
