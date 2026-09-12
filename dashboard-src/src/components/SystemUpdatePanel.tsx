import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogMedia, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Progress, ProgressLabel, ProgressValue } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { AlertTriangle, CheckCircle2, Clock3, RefreshCw, Route, ServerCog, XCircle } from "lucide-react";
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
  if (status === "succeeded") return <CheckCircle2 aria-hidden="true" />;
  if (status === "failed" || status === "interrupted") return <XCircle aria-hidden="true" />;
  if (status === "attention" || status === "skipped") return <AlertTriangle aria-hidden="true" />;
  if (status === "running") return <Spinner aria-hidden="true" />;
  return <Clock3 aria-hidden="true" />;
}

function statusVariant(status?: string) {
  if (status === "succeeded") return "success" as const;
  if (status === "failed" || status === "interrupted") return "destructive" as const;
  if (status === "attention") return "warning" as const;
  return "secondary" as const;
}

function statusLabel(status: string | undefined, zh: boolean) {
  const labels: Record<string, [string, string]> = {
    none: ["未开始", "Not started"],
    idle: ["未开始", "Not started"],
    pending: ["等待中", "Pending"],
    queued: ["已排队", "Queued"],
    running: ["进行中", "Running"],
    succeeded: ["已完成", "Completed"],
    attention: ["需要处理", "Attention needed"],
    failed: ["失败", "Failed"],
    interrupted: ["已中断", "Interrupted"],
    skipped: ["已跳过", "Skipped"],
  };
  return labels[status || "idle"]?.[zh ? 0 : 1] || status;
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
  const [loading, setLoading] = useState(true);
  const [statusLoadError, setStatusLoadError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);

  const targetVersion = versionFromRef(installRef);
  const managedNodes = useMemo(() => nodes.filter((node) => node.agentStatus !== "missing"), [nodes]);
  const nonManagerNodes = useMemo(() => managedNodes.filter((node) => !managerNode(node)), [managedNodes]);
  const staleNodes = useMemo(
    () => nonManagerNodes.filter((node) => !targetVersion || node.agentVersion !== targetVersion).map((node) => node.name || "").filter(Boolean),
    [nonManagerNodes, targetVersion],
  );
  const aligned = managedNodes.filter((node) => targetVersion && node.agentVersion === targetVersion).length;
  const imageRunning = busy === "image" || imagePreparation?.status === "queued" || imagePreparation?.status === "running";
  const managerRunning = busy === "manager" || manager?.status === "running";
  const fleetRunning = busy === "fleet" || fleet?.status === "queued" || fleet?.status === "running";
  const fleetTotal = fleet?.nodes?.length || fleet?.result?.total || 0;
  const fleetCompleted = fleet?.nodes?.length
    ? fleet.nodes.filter((node) => ["succeeded", "failed", "skipped"].includes(node.status || "")).length
    : (fleet?.result?.succeeded || 0) + (fleet?.result?.failed || 0) + (fleet?.result?.skipped || 0);

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
    setLoading(true);
    setStatusLoadError("");
    void Promise.allSettled([
      getManagerUpdate(token, "", controller.signal),
      getControlImagePreparation(token, "", controller.signal),
      listFleetUpdates(token, controller.signal),
    ]).then(([managerResult, imageResult, fleetResult]) => {
      if (controller.signal.aborted) return;
      if (managerResult.status === "fulfilled") setManager(managerResult.value.status === "none" ? null : managerResult.value);
      if (imageResult.status === "fulfilled") setImagePreparation(imageResult.value.status === "none" ? null : imageResult.value);
      if (fleetResult.status === "fulfilled") setFleet(fleetResult.value.operations?.[0] || null);
      const failure = [managerResult, imageResult, fleetResult].find((result) => result.status === "rejected");
      if (failure?.status === "rejected") setStatusLoadError(String(failure.reason instanceof Error ? failure.reason.message : failure.reason));
      setLoading(false);
    });
    return () => controller.abort();
  }, [loadAttempt, token]);

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
      setBaseline(result);
      setConfirm("manager");
      return;
    }
    setConfirm("");
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
    setConfirm("");
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
    <section className="flex min-w-0 flex-col gap-6" aria-labelledby="system-update-title">
      <Card>
        <CardHeader>
          <CardTitle id="system-update-title">{zh ? "升级配置" : "Update configuration"}</CardTitle>
          <CardDescription>{zh ? "升级进度会自动保存，重新打开页面后可继续查看。" : "Rollout progress is saved automatically and remains available when you reopen this page."}</CardDescription>
          <CardAction><Badge variant="outline">Control {controlVersion || "-"}</Badge></CardAction>
        </CardHeader>
        <CardContent>
          <FieldGroup className="grid gap-4 md:grid-cols-2">
            <Field data-disabled={Boolean(busy)}>
              <FieldLabel htmlFor="system-install-ref">{zh ? "目标发布版本" : "Release ref"}</FieldLabel>
              <Input id="system-install-ref" value={installRef} onChange={(event) => onRefChange(event.target.value)} placeholder="v0.1.358" spellCheck={false} disabled={Boolean(busy)} />
            </Field>
            <Field data-disabled={Boolean(busy)}>
              <FieldLabel htmlFor="system-control-image">{zh ? "Control 镜像" : "Control image"}</FieldLabel>
              <Input id="system-control-image" value={controlImage} onChange={(event) => { setControlImage(event.target.value); setConfirm(""); }} placeholder="ghcr.io/liutianjie/luma-control:v0.1.358" spellCheck={false} disabled={Boolean(busy)} />
            </Field>
          </FieldGroup>
        </CardContent>
        <CardFooter className="flex-wrap gap-2">
          <Button type="button" disabled={loading || Boolean(statusLoadError) || !installRef.trim() || !controlImage.trim() || Boolean(busy) || managerRunning || imageRunning} onClick={() => void requestManagerUpdate()}>
            {managerRunning || imageRunning ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <ServerCog data-icon="inline-start" />}
            {imageRunning ? (zh ? "正在准备内网镜像" : "Preparing internal image") : managerRunning ? (zh ? "正在升级 Control…" : "Updating Control…") : (zh ? "升级 Control" : "Update Control")}
          </Button>
          <Button type="button" variant="outline" disabled={loading || Boolean(statusLoadError) || !installRef.trim() || staleNodes.length === 0 || Boolean(busy) || fleetRunning} onClick={() => void requestFleetUpdate()}>
            {fleetRunning ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <RefreshCw data-icon="inline-start" />}
            {fleetRunning ? (zh ? "正在更新节点…" : "Updating nodes…") : staleNodes.length === 0 ? (zh ? "节点已全部对齐" : "Fleet aligned") : (zh ? "更新未对齐节点" : "Update stale nodes")}
          </Button>
          <Button variant="outline" type="button" disabled={Boolean(busy)} onClick={() => void probeRoutes()}>
            {busy === "sentinel" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <Route data-icon="inline-start" />}
            {busy === "sentinel" ? (zh ? "正在检查路由…" : "Checking routes…") : (zh ? "检查全部公网路由" : "Check public routes")}
          </Button>
        </CardFooter>
      </Card>

      <AlertDialog open={confirm !== ""} onOpenChange={(open) => { if (!open) setConfirm(""); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogMedia><AlertTriangle /></AlertDialogMedia>
            <AlertDialogTitle>{confirm === "manager" ? (zh ? "确认升级 Control？" : "Update Control?") : (zh ? `确认更新 ${staleNodes.length} 台节点？` : `Update ${staleNodes.length} nodes?`)}</AlertDialogTitle>
            <AlertDialogDescription>
              {confirm === "manager"
                ? (zh ? `Control 会短暂重连，完成后自动再次检查路由。${baseline ? `本次基线：${baseline.succeeded || 0} 条正常，${baseline.failed || 0} 条异常。` : "本次路由基线检查未完成，请核对页面中的错误。"}` : `Control will reconnect briefly, then routes will be checked again. ${baseline ? `Baseline: ${baseline.succeeded || 0} healthy, ${baseline.failed || 0} failed.` : "The route baseline could not be completed. Review the error on this page."}`)
                : (zh ? `将更新 ${staleNodes.length} 台节点到 ${installRef}，各节点结果会分别显示。` : `Update ${staleNodes.length} nodes to ${installRef}. Each node will report its own result.`)}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{zh ? "取消" : "Cancel"}</AlertDialogCancel>
            <AlertDialogAction variant={confirm === "manager" ? "destructive" : "default"} onClick={(event) => { event.preventDefault(); void (confirm === "manager" ? requestManagerUpdate() : requestFleetUpdate()); }}>
              {zh ? "确认升级" : "Confirm update"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {error ? <Alert variant="destructive"><XCircle /><AlertTitle>{zh ? "操作失败" : "Operation failed"}</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}
      {statusLoadError ? <Alert variant="destructive"><XCircle /><AlertTitle>{zh ? "未能读取升级状态" : "Could not load update status"}</AlertTitle><AlertDescription className="flex flex-col items-start gap-3"><p>{zh ? "重新读取现有任务后即可继续升级，避免重复启动正在进行的任务。" : "Reload existing operations before starting an update to avoid starting a duplicate operation."}</p><p>{statusLoadError}</p><Button type="button" variant="outline" disabled={loading} onClick={() => setLoadAttempt((attempt) => attempt + 1)}><RefreshCw data-icon="inline-start" />{zh ? "重新读取状态" : "Reload status"}</Button></AlertDescription></Alert> : null}
      {reconnecting ? <Alert role="status"><Spinner aria-hidden="true" /><AlertTitle>{zh ? "正在重连 Control" : "Reconnecting to Control"}</AlertTitle><AlertDescription>{zh ? "Control 正在切换，页面会自动恢复连接。" : "Control is switching. This page will reconnect automatically."}</AlertDescription></Alert> : null}

      {!statusLoadError ? <div className="grid min-w-0 items-start gap-4 xl:grid-cols-2" aria-busy={loading}>
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{zh ? "控制面" : "Control plane"}</CardTitle>
            <CardDescription>{zh ? "先检查路由基线，再准备镜像并滚动替换 Control。" : "Check the route baseline, prepare the image, then roll Control."}</CardDescription>
            <CardAction>{loading ? <Skeleton className="h-5 w-16" /> : <Badge variant={statusVariant(manager?.status)}>{statusIcon(manager?.status)}{statusLabel(manager?.status, zh)}</Badge>}</CardAction>
          </CardHeader>
          <CardContent className="flex min-w-0 flex-col gap-4">
            {loading ? <><Skeleton className="h-4 w-3/4" /><Skeleton className="h-4 w-1/2" /></> : <>
              {manager ? <p className="break-words">{manager.message || manager.installRef || "-"}</p> : <Empty><EmptyHeader><EmptyMedia variant="icon"><ServerCog /></EmptyMedia><EmptyTitle>{zh ? "尚未开始升级" : "No update started"}</EmptyTitle><EmptyDescription>{zh ? "填写目标版本后，选择升级 Control。" : "Enter a release ref, then choose Update Control."}</EmptyDescription></EmptyHeader></Empty>}
              {imagePreparation && imagePreparation.status !== "none" ? (
                <Alert variant={imagePreparation.status === "failed" || imagePreparation.status === "interrupted" ? "destructive" : "default"} role="status">
                  {statusIcon(imagePreparation.status)}
                  <AlertTitle>{zh ? "内网镜像" : "Internal image"} · {statusLabel(imagePreparation.status, zh)}</AlertTitle>
                  <AlertDescription className="break-all">{imagePreparation.message || imagePreparation.destinationImage || "-"}</AlertDescription>
                </Alert>
              ) : null}
              {manager?.log?.length ? <ScrollArea className="h-40" aria-label={zh ? "Control 升级日志" : "Control update log"}><pre className="whitespace-pre-wrap break-all font-mono text-xs"><code>{manager.log.slice(-8).join("\n")}</code></pre></ScrollArea> : null}
            </>}
          </CardContent>
        </Card>
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{zh ? "节点更新" : "Node updates"}</CardTitle>
            <CardDescription>{targetVersion ? (zh ? `${aligned}/${managedNodes.length} 台受管节点已对齐；${staleNodes.length} 台工作节点待更新。` : `${aligned}/${managedNodes.length} managed nodes aligned; ${staleNodes.length} worker nodes need an update.`) : (zh ? "填写目标版本以检查节点是否对齐。" : "Enter a release ref to check node versions.")}</CardDescription>
            <CardAction>{loading ? <Skeleton className="h-5 w-16" /> : <Badge variant={statusVariant(fleet?.status)}>{statusIcon(fleet?.status)}{statusLabel(fleet?.status, zh)}</Badge>}</CardAction>
          </CardHeader>
          <CardContent className="flex min-w-0 flex-col gap-4">
            {loading ? <><Skeleton className="h-4 w-3/4" /><Skeleton className="h-4 w-1/2" /></> : <>
              {fleet?.message ? <p className="break-words">{fleet.message}</p> : null}
              {fleetTotal > 0 ? <Progress value={Math.min(fleetCompleted, fleetTotal)} max={fleetTotal}><ProgressLabel>{zh ? "已处理节点" : "Processed nodes"}</ProgressLabel><ProgressValue>{() => `${fleetCompleted}/${fleetTotal}`}</ProgressValue></Progress> : null}
              {fleet?.result ? <div className="flex flex-wrap gap-2"><Badge variant="success">{zh ? "成功" : "Succeeded"} {fleet.result.succeeded || 0}</Badge><Badge variant={fleet.result.failed ? "destructive" : "secondary"}>{zh ? "失败" : "Failed"} {fleet.result.failed || 0}</Badge><Badge variant="secondary">{zh ? "跳过" : "Skipped"} {fleet.result.skipped || 0}</Badge></div> : null}
              {fleet?.nodes?.length ? (
                <Table aria-label={zh ? "节点更新结果" : "Node update results"} containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "节点更新列表" : "Node update list" }}>
                  <TableHeader><TableRow><TableHead>{zh ? "节点" : "Node"}</TableHead><TableHead>{zh ? "状态" : "Status"}</TableHead><TableHead>{zh ? "说明" : "Details"}</TableHead></TableRow></TableHeader>
                  <TableBody>{fleet.nodes.map((node) => <TableRow key={node.nodeName}><TableCell>{node.nodeName || "-"}</TableCell><TableCell><Badge variant={statusVariant(node.status)}>{statusIcon(node.status)}{statusLabel(node.status, zh)}</Badge></TableCell><TableCell className="min-w-40 whitespace-normal break-words">{node.message || "-"}</TableCell></TableRow>)}</TableBody>
                </Table>
              ) : !fleet ? <Empty><EmptyHeader><EmptyMedia variant="icon"><ServerCog /></EmptyMedia><EmptyTitle>{zh ? "暂无节点更新记录" : "No node updates yet"}</EmptyTitle><EmptyDescription>{zh ? "开始更新后，可以在这里查看每台节点的结果。" : "After starting an update, each node’s result will appear here."}</EmptyDescription></EmptyHeader></Empty> : null}
            </>}
          </CardContent>
        </Card>
      </div> : null}

      {sentinel ? (
        <Card>
          <CardHeader>
            <CardTitle>{zh ? "最近一次路由检查" : "Latest route check"}</CardTitle>
            <CardDescription>{sentinel.checkedAt ? new Date(sentinel.checkedAt * 1000).toLocaleString(zh ? "zh-CN" : "en-US") : (zh ? "当前会话的检查结果。" : "Results from this session.")}</CardDescription>
            <CardAction><Badge variant={sentinel.failed ? "warning" : "secondary"}>{sentinel.succeeded || 0}/{sentinel.total || 0} {zh ? "可达" : "reachable"}</Badge></CardAction>
          </CardHeader>
          <CardContent>
            {sentinel.results?.length ? <Table aria-label={zh ? "公网路由检查结果" : "Public route check results"} containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "公网路由检查列表" : "Public route check list" }}>
              <TableHeader><TableRow><TableHead>{zh ? "域名" : "Domain"}</TableHead><TableHead>{zh ? "状态" : "Status"}</TableHead><TableHead>{zh ? "响应" : "Response"}</TableHead><TableHead>{zh ? "耗时" : "Latency"}</TableHead></TableRow></TableHeader>
              <TableBody>{sentinel.results.map((result) => <TableRow key={result.domain}><TableCell>{result.domain || "-"}</TableCell><TableCell><Badge variant={result.ok ? "success" : "destructive"}>{result.ok ? <CheckCircle2 /> : <XCircle />}{result.ok ? (zh ? "可达" : "Reachable") : (zh ? "异常" : "Failed")}</Badge></TableCell><TableCell className="min-w-32 whitespace-normal break-words">{result.status || result.error || "-"}</TableCell><TableCell>{result.latencyMs ?? "-"} ms</TableCell></TableRow>)}</TableBody>
            </Table> : <Empty><EmptyHeader><EmptyMedia variant="icon"><Route /></EmptyMedia><EmptyTitle>{zh ? "暂无公网路由" : "No public routes"}</EmptyTitle><EmptyDescription>{zh ? "此次检查没有返回可验证的路由。" : "This check returned no routes to verify."}</EmptyDescription></EmptyHeader></Empty>}
          </CardContent>
        </Card>
      ) : null}
    </section>
  );
}
