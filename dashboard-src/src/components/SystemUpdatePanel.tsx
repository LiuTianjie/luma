import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, LoaderCircle, RefreshCw, Route, ServerCog, XCircle } from "lucide-react";
import type { DashboardNode, Lang } from "../types";
import {
  getControlImagePreparation,
  getFleetUpdate,
  getManagerUpdate,
  listFleetUpdates,
  runRouteSentinel,
  startControlImagePreparation,
  startFleetUpdate,
  startManagerUpdate,
  type ControlImagePreparation,
  type FleetUpdateOperation,
  type ManagerUpdate,
  type RouteSentinel,
} from "../systemUpdateApi";

function versionFromRef(value: string) {
  return value.trim().replace(/^v/, "");
}

function defaultControlImage(value: string) {
  const ref = value.trim();
  if (/^v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$/.test(ref)) return `ghcr.io/liutianjie/luma-control:${ref}`;
  if (/^[a-f0-9]{40}$/.test(ref)) return `ghcr.io/liutianjie/luma-control:sha-${ref.slice(0, 7)}`;
  return "";
}

function managerNode(node: DashboardNode) {
  return Boolean(node.leader) || (node.role || "").toLowerCase().includes("manager");
}

function terminalStatus(status?: string) {
  return ["succeeded", "attention", "failed", "interrupted"].includes(status || "");
}

function statusIcon(status?: string) {
  if (status === "succeeded") return <CheckCircle2 size={15} aria-hidden="true" />;
  if (status === "failed" || status === "interrupted") return <XCircle size={15} aria-hidden="true" />;
  if (status === "attention" || status === "skipped") return <AlertTriangle size={15} aria-hidden="true" />;
  return <LoaderCircle className="spin" size={15} aria-hidden="true" />;
}

export function SystemUpdatePanel({
  lang,
  token,
  controlVersion,
  nodes,
  onRefresh,
}: {
  lang: Lang;
  token: string;
  controlVersion: string;
  nodes: DashboardNode[];
  onRefresh: () => Promise<void> | void;
}) {
  const zh = lang === "zh";
  const initialRef = controlVersion ? `v${controlVersion}` : "";
  const [installRef, setInstallRef] = useState(initialRef);
  const [controlImage, setControlImage] = useState(defaultControlImage(initialRef));
  const [manager, setManager] = useState<ManagerUpdate | null>(null);
  const [imagePreparation, setImagePreparation] = useState<ControlImagePreparation | null>(null);
  const [fleet, setFleet] = useState<FleetUpdateOperation | null>(null);
  const [sentinel, setSentinel] = useState<RouteSentinel | null>(null);
  const [baseline, setBaseline] = useState<RouteSentinel | null>(null);
  const [busy, setBusy] = useState<"manager" | "image" | "fleet" | "sentinel" | "">("");
  const [confirm, setConfirm] = useState<"manager" | "fleet" | "">("");
  const [error, setError] = useState("");
  const [reconnecting, setReconnecting] = useState(false);

  const targetVersion = versionFromRef(installRef);
  const managedNodes = useMemo(() => nodes.filter((node) => node.agentStatus !== "missing"), [nodes]);
  const nonManagerNodes = useMemo(() => managedNodes.filter((node) => !managerNode(node)), [managedNodes]);
  const staleNodes = useMemo(
    () => nonManagerNodes.filter((node) => !targetVersion || node.agentVersion !== targetVersion).map((node) => node.name || "").filter(Boolean),
    [nonManagerNodes, targetVersion],
  );
  const aligned = managedNodes.filter((node) => targetVersion && node.agentVersion === targetVersion).length;

  const probeRoutes = useCallback(async (domains?: string[]) => {
    setBusy("sentinel");
    setError("");
    try {
      const result = await runRouteSentinel(token, domains);
      setSentinel(result);
      return result;
    } catch (nextError) {
      setError(String(nextError instanceof Error ? nextError.message : nextError));
      return null;
    } finally {
      setBusy("");
    }
  }, [token]);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      getManagerUpdate(token, "", controller.signal).catch(() => null),
      getControlImagePreparation(token, "", controller.signal).catch(() => null),
      listFleetUpdates(token, controller.signal).catch(() => ({ operations: [] })),
    ]).then(([managerResult, imageResult, fleetResult]) => {
      if (managerResult && managerResult.status !== "none") setManager(managerResult);
      if (imageResult && imageResult.status !== "none") setImagePreparation(imageResult);
      const latest = fleetResult.operations?.[0];
      if (latest) setFleet(latest);
    });
    return () => controller.abort();
  }, [token]);

  useEffect(() => {
    const managerRunning = manager?.status === "running";
    const imageRunning = imagePreparation?.status === "queued" || imagePreparation?.status === "running";
    const fleetRunning = fleet?.status === "queued" || fleet?.status === "running";
    if (!managerRunning && !imageRunning && !fleetRunning) return;
    const timer = window.setInterval(() => {
      if (managerRunning) {
        void getManagerUpdate(token, manager?.updateId || "")
          .then((result) => {
            setManager(result);
            setReconnecting(false);
            if (result.status === "succeeded") {
              const baselineDomains = (baseline?.results || [])
                .filter((route) => route.ok && route.domain)
                .map((route) => route.domain as string);
              void probeRoutes(baselineDomains.length ? baselineDomains : undefined);
              void onRefresh();
            }
          })
          .catch(() => setReconnecting(true));
      }
      if (imageRunning && imagePreparation?.id) {
        void getControlImagePreparation(token, imagePreparation.id)
          .then((result) => {
            setImagePreparation(result);
            setReconnecting(false);
            if (result.status === "succeeded") {
              const preparedImage = result.result?.destinationImage || result.destinationImage;
              if (preparedImage) setControlImage(preparedImage);
            }
          })
          .catch(() => setReconnecting(true));
      }
      if (fleetRunning && fleet?.id) {
        void getFleetUpdate(token, fleet.id)
          .then((result) => {
            setFleet(result);
            setReconnecting(false);
            if (terminalStatus(result.status)) void onRefresh();
          })
          .catch(() => setReconnecting(true));
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [baseline?.results, fleet?.id, fleet?.status, imagePreparation?.id, imagePreparation?.status, manager?.status, manager?.updateId, onRefresh, probeRoutes, token]);

  const requestManagerUpdate = async () => {
    if (confirm !== "manager") {
      const result = await probeRoutes();
      if (result) setBaseline(result);
      setConfirm("manager");
      return;
    }
    setBusy("image");
    setError("");
    try {
      let prepared = await startControlImagePreparation(token, installRef.trim(), controlImage.trim());
      setImagePreparation(prepared);
      for (let attempt = 0; ["queued", "running"].includes(prepared.status || "") && attempt < 450; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 2000));
        prepared = await getControlImagePreparation(token, prepared.id || "");
        setImagePreparation(prepared);
      }
      if (prepared.status !== "succeeded") {
        throw new Error(prepared.message || (zh ? "内网镜像准备失败" : "Internal image preparation failed"));
      }
      const preparedImage = prepared.result?.destinationImage || prepared.destinationImage || controlImage.trim();
      setControlImage(preparedImage);
      setBusy("manager");
      const result = await startManagerUpdate(token, installRef.trim(), preparedImage);
      setManager(result);
      setConfirm("");
    } catch (nextError) {
      setError(String(nextError instanceof Error ? nextError.message : nextError));
    } finally {
      setBusy("");
    }
  };

  const requestFleetUpdate = async () => {
    if (confirm !== "fleet") {
      setConfirm("fleet");
      return;
    }
    setBusy("fleet");
    setError("");
    try {
      const result = await startFleetUpdate(token, installRef.trim(), staleNodes);
      setFleet(result);
      setConfirm("");
    } catch (nextError) {
      setError(String(nextError instanceof Error ? nextError.message : nextError));
    } finally {
      setBusy("");
    }
  };

  const onRefChange = (value: string) => {
    setInstallRef(value);
    const suggested = defaultControlImage(value);
    if (suggested) setControlImage(suggested);
    setConfirm("");
  };

  return (
    <section className="system-update-panel panel" aria-labelledby="system-update-title">
      <div className="system-update-heading">
        <div>
          <p className="eyebrow">{zh ? "升级中心" : "Update center"}</p>
          <h2 id="system-update-title">{zh ? "控制面、节点与路由一次看清" : "Control, fleet, and route rollout"}</h2>
          <p>{zh ? "升级过程持久化，页面关闭或 Control 重连后仍可恢复；不需要登录服务器执行命令。" : "Rollouts persist across page closes and Control reconnects; no server shell is required."}</p>
        </div>
        <div className="system-update-version">
          <span>{zh ? "当前 Control" : "Control"}</span>
          <strong>{controlVersion || "-"}</strong>
          <small>{targetVersion ? `${aligned}/${managedNodes.length} ${zh ? "受管节点已对齐" : "managed nodes aligned"}` : "-"}</small>
        </div>
      </div>

      <div className="system-update-form">
        <label>
          <span>{zh ? "目标发布版本" : "Release ref"}</span>
          <input value={installRef} onChange={(event) => onRefChange(event.target.value)} placeholder="v0.1.174" spellCheck={false} />
        </label>
        <label>
          <span>{zh ? "Control 镜像" : "Control image"}</span>
          <input value={controlImage} onChange={(event) => setControlImage(event.target.value)} placeholder="ghcr.io/liutianjie/luma-control:v0.1.174" spellCheck={false} />
        </label>
      </div>

      <div className="system-update-actions">
        <button type="button" className="ghost" disabled={Boolean(busy)} onClick={() => void probeRoutes()}>
          {busy === "sentinel" ? <LoaderCircle className="spin" size={16} /> : <Route size={16} />}
          {zh ? "检查全部公网路由" : "Check public routes"}
        </button>
        <button type="button" className={confirm === "manager" ? "danger" : "secondary"} disabled={!installRef.trim() || !controlImage.trim() || Boolean(busy) || manager?.status === "running" || imagePreparation?.status === "queued" || imagePreparation?.status === "running"} onClick={() => void requestManagerUpdate()}>
          {busy === "manager" || busy === "image" ? <LoaderCircle className="spin" size={16} /> : <ServerCog size={16} />}
          {busy === "image" || imagePreparation?.status === "queued" || imagePreparation?.status === "running" ? (zh ? "正在准备内网镜像" : "Preparing internal image") : confirm === "manager" ? (zh ? "确认升级 Control" : "Confirm Control update") : (zh ? "升级 Control" : "Update Control")}
        </button>
        <button type="button" className={confirm === "fleet" ? "primary" : "secondary"} disabled={!installRef.trim() || staleNodes.length === 0 || Boolean(busy) || fleet?.status === "running" || fleet?.status === "queued"} onClick={() => void requestFleetUpdate()}>
          {busy === "fleet" ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}
          {staleNodes.length === 0 ? (zh ? "节点已全部对齐" : "Fleet aligned") : confirm === "fleet" ? (zh ? `确认更新 ${staleNodes.length} 台节点` : `Confirm ${staleNodes.length} nodes`) : (zh ? "更新未对齐节点" : "Update stale nodes")}
        </button>
      </div>

      {confirm === "manager" ? (
        <div className="system-update-confirm" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          <span>{zh ? `升级会短暂重连 Control。基线检查：${baseline?.succeeded || 0} 正常，${baseline?.failed || 0} 异常；完成后会自动再次检查。` : `Control will reconnect briefly. Baseline: ${baseline?.succeeded || 0} healthy, ${baseline?.failed || 0} failed; routes are checked again automatically.`}</span>
          <button type="button" className="ghost" onClick={() => setConfirm("")}>{zh ? "取消" : "Cancel"}</button>
        </div>
      ) : null}

      {error ? <div className="system-update-error" role="alert"><XCircle size={16} />{error}</div> : null}
      {reconnecting ? <div className="system-update-reconnect"><LoaderCircle className="spin" size={15} />{zh ? "Control 正在切换，页面会自动重连…" : "Control is switching; reconnecting automatically…"}</div> : null}

      <div className="system-update-progress-grid">
        <article>
          <header><span>{zh ? "控制面" : "Control plane"}</span><strong className={`update-status ${manager?.status || "idle"}`}>{manager ? statusIcon(manager.status) : null}{manager?.status || (zh ? "未开始" : "idle")}</strong></header>
          <p>{manager?.message || manager?.installRef || (zh ? "先运行路由基线，再滚动替换 Control。" : "Capture a route baseline, then roll Control.")}</p>
          {imagePreparation && imagePreparation.status !== "none" ? (
            <div className={`system-update-image-progress ${imagePreparation.status || "idle"}`}>
              {statusIcon(imagePreparation.status)}
              <span><b>{zh ? "内网镜像" : "Internal image"}</b><small>{imagePreparation.message || imagePreparation.destinationImage || "-"}</small></span>
            </div>
          ) : null}
          {manager?.log?.length ? <pre><code>{manager.log.slice(-8).join("\n")}</code></pre> : null}
        </article>
        <article>
          <header><span>{zh ? "节点舰队" : "Fleet"}</span><strong className={`update-status ${fleet?.status || "idle"}`}>{fleet ? statusIcon(fleet.status) : null}{fleet?.status || (zh ? "未开始" : "idle")}</strong></header>
          <p>{fleet?.result ? `${fleet.result.succeeded || 0} ok · ${fleet.result.failed || 0} failed · ${fleet.result.skipped || 0} skipped` : (zh ? `${staleNodes.length} 台节点待对齐` : `${staleNodes.length} nodes need alignment`)}</p>
          {fleet?.nodes?.length ? (
            <div className="system-update-node-list">
              {fleet.nodes.map((node) => <span key={node.nodeName} className={node.status || "pending"}>{statusIcon(node.status)}<b>{node.nodeName}</b><small>{node.message || node.status}</small></span>)}
            </div>
          ) : null}
        </article>
      </div>

      {sentinel ? (
        <div className="route-sentinel-summary">
          <header><strong>{zh ? "最近一次路由检查" : "Latest route sentinel"}</strong><span>{sentinel.succeeded || 0}/{sentinel.total || 0} {zh ? "可达" : "reachable"}</span></header>
          <div>
            {(sentinel.results || []).map((result) => (
              <span className={result.ok ? "ok" : "failed"} key={result.domain}>{result.ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />}<b>{result.domain}</b><small>{result.status || result.error || "-"} · {result.latencyMs || 0} ms</small></span>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}
