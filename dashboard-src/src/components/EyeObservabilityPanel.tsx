import { useCallback, useEffect, useState } from "react";
import { AlertCircle, ArrowUpRight, Check, Copy, ExternalLink, Package, RefreshCw } from "@/components/icons";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { apiGet } from "../apiClient";
import type { Lang } from "../types";
import { AddonRequiredCard } from "./AddonRequiredCard";

type EyeProjectEntry = {
  slug: string;
  name: string;
  publicKey: string;
  allowedOrigins: string[];
  consoleUrl: string;
  uniqueVisitors: number;
  pageviews: number;
  errorCount: number;
  crashFreeRate: number;
};

type EyePortfolioPayload = {
  available: boolean;
  installed: boolean;
  reason?: string;
  error?: string;
  consoleUrl: string;
  eyeUrl?: string;
  since: string;
  totals?: {
    projects: number;
    uniqueVisitors: number;
    pageviews: number;
    errorCount: number;
  };
  projects: EyeProjectEntry[];
};

type EyeAppPayload = {
  available: boolean;
  installed: boolean;
  reason?: string;
  error?: string;
  app: string;
  since: string;
  consoleUrl: string;
  eyeUrl?: string;
  matched: boolean;
  project: EyeProjectEntry | null;
  projectsCount: number;
};

const WINDOWS = ["1h", "24h", "7d", "30d"] as const;

function formatCount(value: number): string {
  return new Intl.NumberFormat().format(value);
}

export function EyeObservabilityPanel({
  lang,
  token,
  app = "",
}: {
  lang: Lang;
  token: string;
  app?: string;
}) {
  const zh = lang === "zh";
  const [since, setSince] = useState<(typeof WINDOWS)[number]>("24h");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [portfolio, setPortfolio] = useState<EyePortfolioPayload | null>(null);
  const [appData, setAppData] = useState<EyeAppPayload | null>(null);
  const [copiedKey, setCopiedKey] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      if (app) {
        const res = await apiGet<EyeAppPayload>(
          `/v1/dashboard/addons/eye/app?${new URLSearchParams({ app, since }).toString()}`,
          token,
        );
        setAppData(res);
      } else {
        const res = await apiGet<EyePortfolioPayload>(
          `/v1/dashboard/addons/eye/portfolio?${new URLSearchParams({ since }).toString()}`,
          token,
        );
        setPortfolio(res);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [app, since, token]);

  useEffect(() => {
    void load();
  }, [load]);

  const copyText = async (id: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopiedKey(id);
      window.setTimeout(() => setCopiedKey(""), 1800);
    } catch {
      // clipboard unavailable
    }
  };

  const activeData = app ? appData : portfolio;
  if (!loading && activeData && !activeData.installed) {
    return <AddonRequiredCard addonId="eye" lang={lang} />;
  }

  const toolbar = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <ToggleGroup
        variant="segmented"
        size="sm"
        value={[since]}
        onValueChange={(values) => {
          const next = values[0] as (typeof WINDOWS)[number] | undefined;
          if (next && WINDOWS.includes(next)) setSince(next);
        }}
        aria-label={zh ? "统计时间范围" : "Time window"}
      >
        {WINDOWS.map((w) => (
          <ToggleGroupItem key={w} value={w}>
            {w}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <div className="flex items-center gap-2">
        <Button variant="outline" size="sm" disabled={loading} onClick={() => void load()}>
          <RefreshCw className="size-3.5" aria-hidden="true" />
          {zh ? "刷新" : "Refresh"}
        </Button>
        {activeData?.consoleUrl ? (
          <Button
            size="sm"
            variant="outline"
            nativeButton={false}
            render={<a href={activeData.consoleUrl} target="_blank" rel="noreferrer" />}
          >
            {zh ? "打开 Luma Eye 控制台" : "Open Luma Eye console"}
            <ArrowUpRight className="ml-1 size-3.5" aria-hidden="true" />
          </Button>
        ) : null}
      </div>
    </div>
  );

  if (error || (activeData && activeData.installed && !activeData.available)) {
    return (
      <div className="flex min-w-0 flex-col gap-4">
        {toolbar}
        <Alert variant="warning">
          <AlertCircle className="size-4" />
          <AlertTitle>{zh ? "暂时无法连接到 Luma Eye" : "Luma Eye is temporarily unreachable"}</AlertTitle>
          <AlertDescription className="wrap-anywhere">
            {error || activeData?.error || (zh ? "请检查 Luma Eye 服务是否运行正常，或直接打开其控制台查看。" : "Check that Luma Eye is running or open its console directly.")}
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  if (loading && !activeData) {
    return (
      <div className="flex min-w-0 flex-col gap-4">
        {toolbar}
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Skeleton className="h-24 w-full rounded-xl" />
          <Skeleton className="h-24 w-full rounded-xl" />
          <Skeleton className="h-24 w-full rounded-xl" />
          <Skeleton className="h-24 w-full rounded-xl" />
        </div>
      </div>
    );
  }

  // Application-scoped view
  if (app && appData) {
    if (!appData.matched || !appData.project) {
      const endpoint = appData.eyeUrl || "https://eye.example.com";
      const snippet = `import { initLumaEye } from "@nickname4th/luma-eye";\ninitLumaEye({ endpoint: "${endpoint}", project: "${app}" });`;
      return (
        <div className="flex min-w-0 flex-col gap-4">
          {toolbar}
          <Empty>
            <EmptyHeader>
              <EmptyMedia variant="icon"><Package /></EmptyMedia>
              <EmptyTitle>{zh ? `尚未在 Luma Eye 中匹配到 ${app}` : `No Luma Eye project matched ${app}`}</EmptyTitle>
              <EmptyDescription>
                {zh
                  ? `Luma 会按应用名（${app}）或公网域名自动匹配 Eye 项目。安装前端 SDK 并上报首条事件，或在 Eye 控制台创建同名项目后即可在此查看实时 UV/PV 与崩溃率。`
                  : `Luma automatically matches Eye projects by application name (${app}) or domain. Instrument your frontend SDK or create a matching project in Luma Eye to view UV/PV and crash-free metrics here.`}
              </EmptyDescription>
            </EmptyHeader>
            <EmptyContent className="flex w-full max-w-xl flex-col gap-3">
              <div className="flex items-start justify-between gap-2 rounded-lg border border-border bg-muted/40 p-3 text-left font-mono text-xs text-muted-foreground">
                <pre className="m-0 overflow-x-auto">{snippet}</pre>
                <button
                  type="button"
                  onClick={() => void copyText("snippet", snippet)}
                  className="shrink-0 text-muted-foreground hover:text-foreground"
                  aria-label={zh ? "复制 SDK 示例" : "Copy SDK snippet"}
                >
                  {copiedKey === "snippet" ? <Check className="size-3.5 text-emerald-600" /> : <Copy className="size-3.5" />}
                </button>
              </div>
              {appData.consoleUrl ? (
                <Button size="sm" nativeButton={false} render={<a href={appData.consoleUrl} target="_blank" rel="noreferrer" />}>
                  {zh ? "前往 Luma Eye 创建或管理项目" : "Manage projects in Luma Eye"}
                  <ExternalLink className="ml-1 size-3.5" aria-hidden="true" />
                </Button>
              ) : null}
            </EmptyContent>
          </Empty>
        </div>
      );
    }

    const proj = appData.project;
    return (
      <div className="flex min-w-0 flex-col gap-5">
        {toolbar}
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Card>
            <CardHeader className="pb-2"><CardDescription>{zh ? `访客数 (UV · ${since})` : `Unique visitors (${since})`}</CardDescription></CardHeader>
            <CardContent><div className="text-2xl font-semibold tabular-nums">{formatCount(proj.uniqueVisitors)}</div></CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2"><CardDescription>{zh ? `浏览量 (PV · ${since})` : `Pageviews (${since})`}</CardDescription></CardHeader>
            <CardContent><div className="text-2xl font-semibold tabular-nums">{formatCount(proj.pageviews)}</div></CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2"><CardDescription>{zh ? `客户端报错 (${since})` : `Client errors (${since})`}</CardDescription></CardHeader>
            <CardContent>
              <div className="text-2xl font-semibold tabular-nums">{formatCount(proj.errorCount)}</div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2"><CardDescription>{zh ? "无崩溃会话率" : "Crash-free sessions"}</CardDescription></CardHeader>
            <CardContent>
              <div className="flex items-center gap-2">
                <span className="text-2xl font-semibold tabular-nums">{proj.crashFreeRate.toFixed(1)}%</span>
                <Badge variant={proj.crashFreeRate >= 99 ? "success" : proj.crashFreeRate >= 95 ? "warning" : "destructive"}>
                  {proj.crashFreeRate >= 99 ? (zh ? "健康" : "Healthy") : (zh ? "需关注" : "Attention")}
                </Badge>
              </div>
            </CardContent>
          </Card>
        </div>

        <Card>
          <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle className="text-base">{proj.name}</CardTitle>
              <CardDescription className="font-mono text-xs">{proj.slug}</CardDescription>
            </div>
            <Button size="sm" nativeButton={false} render={<a href={proj.consoleUrl} target="_blank" rel="noreferrer" />}>
              {zh ? "在 Luma Eye 查看完整漏斗、崩溃与回放" : "Open full analytics, crashes & replay in Eye"}
              <ArrowUpRight className="ml-1 size-3.5" aria-hidden="true" />
            </Button>
          </CardHeader>
          <CardContent className="flex flex-col gap-3 text-sm">
            {proj.publicKey ? (
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-muted/35 px-3 py-2">
                <span className="text-xs text-muted-foreground">Public Key</span>
                <div className="flex items-center gap-2">
                  <code className="font-mono text-xs">{proj.publicKey}</code>
                  <button
                    type="button"
                    onClick={() => void copyText("pk", proj.publicKey)}
                    className="text-muted-foreground hover:text-foreground"
                    aria-label={zh ? "复制 Public Key" : "Copy public key"}
                  >
                    {copiedKey === "pk" ? <Check className="size-3.5 text-emerald-600" /> : <Copy className="size-3.5" />}
                  </button>
                </div>
              </div>
            ) : null}
            {proj.allowedOrigins.length ? (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-muted-foreground">{zh ? "允许来源：" : "Allowed origins:"}</span>
                {proj.allowedOrigins.map((origin) => (
                  <Badge key={origin} variant="outline" className="font-mono text-xs">
                    {origin}
                  </Badge>
                ))}
              </div>
            ) : null}
          </CardContent>
        </Card>
      </div>
    );
  }

  // Global portfolio view (/observe/eye)
  const projects = portfolio?.projects || [];
  const totals = portfolio?.totals || { projects: projects.length, uniqueVisitors: 0, pageviews: 0, errorCount: 0 };

  return (
    <div className="flex min-w-0 flex-col gap-5">
      {toolbar}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="pb-2"><CardDescription>{zh ? "已接入项目" : "Connected projects"}</CardDescription></CardHeader>
          <CardContent><div className="text-2xl font-semibold tabular-nums">{formatCount(totals.projects)}</div></CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardDescription>{zh ? `总访客 (UV · ${since})` : `Total UV (${since})`}</CardDescription></CardHeader>
          <CardContent><div className="text-2xl font-semibold tabular-nums">{formatCount(totals.uniqueVisitors)}</div></CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardDescription>{zh ? `总浏览量 (PV · ${since})` : `Total PV (${since})`}</CardDescription></CardHeader>
          <CardContent><div className="text-2xl font-semibold tabular-nums">{formatCount(totals.pageviews)}</div></CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardDescription>{zh ? `客户端报错 (${since})` : `Client errors (${since})`}</CardDescription></CardHeader>
          <CardContent><div className="text-2xl font-semibold tabular-nums">{formatCount(totals.errorCount)}</div></CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{zh ? "前端项目总览" : "Frontend projects"}</CardTitle>
          <CardDescription>
            {zh
              ? "数据实时聚合自 Luma Eye 插件，点击任意项目可免密 SSO 直达崩溃堆栈、Web Vitals 与会话回放。"
              : "Live summary from Luma Eye. Open any project via unified Control SSO for crashes, Web Vitals, and session replay."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {projects.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              {zh ? "Luma Eye 中暂无项目。" : "No projects registered in Luma Eye yet."}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{zh ? "项目" : "Project"}</TableHead>
                  <TableHead className="text-right">UV</TableHead>
                  <TableHead className="text-right">PV</TableHead>
                  <TableHead className="text-right">{zh ? "报错" : "Errors"}</TableHead>
                  <TableHead className="text-right">{zh ? "无崩溃率" : "Crash-free"}</TableHead>
                  <TableHead className="text-right">{zh ? "操作" : "Action"}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {projects.map((proj) => (
                  <TableRow key={proj.slug}>
                    <TableCell>
                      <div className="font-medium">{proj.name}</div>
                      <div className="font-mono text-xs text-muted-foreground">{proj.slug}</div>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{formatCount(proj.uniqueVisitors)}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatCount(proj.pageviews)}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {proj.errorCount > 0 ? <Badge variant="destructive">{formatCount(proj.errorCount)}</Badge> : "0"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      <Badge variant={proj.crashFreeRate >= 99 ? "success" : proj.crashFreeRate >= 95 ? "warning" : "destructive"}>
                        {proj.crashFreeRate.toFixed(1)}%
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        nativeButton={false}
                        render={<a href={proj.consoleUrl} target="_blank" rel="noreferrer" />}
                      >
                        {zh ? "详情" : "Inspect"}
                        <ArrowUpRight className="ml-1 size-3.5" aria-hidden="true" />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
