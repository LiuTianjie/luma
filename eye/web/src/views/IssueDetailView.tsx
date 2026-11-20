import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { AlertCircle, ArrowLeft, ArrowRight } from "@/components/icons";
import { api } from "@/api/client";
import { BreadcrumbTrail } from "@/components/inspectors/BreadcrumbTrail";
import { StackTraceViewer } from "@/components/inspectors/StackTraceViewer";
import { PageHeader } from "@/components/PageHeader";
import { StatePill } from "@/components/primitives";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useWorkspace } from "@/context/WorkspaceContext";
import { useQuery } from "@/hooks/useQuery";
import { paths } from "@/router/paths";
import { formatLocalTime, formatRelativeTime } from "@/utils/format";

export function IssueDetailView() {
  const { projectSlug = "", fingerprint = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const { since, refreshTick, notify } = useWorkspace();

  const [bcFilter, setBcFilter] = useState("all");
  const [statusOverride, setStatusOverride] = useState<"open" | "resolved" | "ignored" | null>(null);

  const occIdx = Math.max(0, Number(searchParams.get("sample") || "0") || 0);
  const rawTab = searchParams.get("tab");
  const activeTab: "breadcrumbs" | "stack" | "context" =
    rawTab === "stack" || rawTab === "context" ? rawTab : "breadcrumbs";

  const updateQuery = (patch: { sample?: number; tab?: "breadcrumbs" | "stack" | "context" }) => {
    const sp = new URLSearchParams(searchParams);
    if (patch.sample !== undefined) {
      if (patch.sample <= 0) sp.delete("sample");
      else sp.set("sample", String(patch.sample));
    }
    if (patch.tab !== undefined) {
      if (patch.tab === "breadcrumbs") sp.delete("tab");
      else sp.set("tab", patch.tab);
    }
    setSearchParams(sp, { replace: true });
  };

  const queryKey =
    projectSlug && fingerprint ? `issue-detail:${projectSlug}:${fingerprint}:${refreshTick}` : null;

  const { data: detail, loading, error } = useQuery(queryKey, (signal) =>
    api.issueDetail(projectSlug, fingerprint, signal),
  );

  const handleStatusChange = async (status: "open" | "resolved" | "ignored") => {
    if (!detail) return;
    try {
      await api.setIssueStatus(projectSlug, detail.summary.fingerprint, status);
      setStatusOverride(status);
      notify(
        `已将该异常状态标记为「${status === "open" ? "待处理" : status === "resolved" ? "已解决" : "已忽略"}」。`,
      );
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), "error");
    }
  };

  if (loading && !detail) {
    return (
      <div className="flex flex-col gap-6">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }

  if (!detail) {
    return (
      <Empty className="py-12">
        <EmptyHeader>
          <EmptyTitle>{error || "未找到该异常指纹详情"}</EmptyTitle>
        </EmptyHeader>
        <Button
          variant="outline"
          size="sm"
          nativeButton={false}
          render={<Link to={paths.projectSection(projectSlug, "issues", since)} />}
        >
          <ArrowLeft data-icon="inline-start" />
          返回异常列表
        </Button>
      </Empty>
    );
  }

  const effectiveStatus = statusOverride || detail.summary.status;
  const occurrences =
    detail.occurrences && detail.occurrences.length > 0
      ? detail.occurrences
      : [
          {
            ts: detail.occurred_at || detail.summary.last_seen,
            actor_id: detail.actor_id,
            session_id: detail.session_id,
            env: detail.env,
            release: detail.release,
            level: detail.summary.level,
            page_url: detail.page_url,
            trace_id: detail.trace_id,
            browser: detail.browser,
            os: detail.os,
            device_type: detail.device_type,
            frames: detail.frames,
            breadcrumbs: detail.breadcrumbs,
            tags: detail.tags,
            extra: detail.extra,
          },
        ];

  const safeIdx = Math.min(occIdx, occurrences.length - 1);
  const currentOcc = occurrences[safeIdx];

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader
        compact
        title={`${detail.summary.error_type}: ${detail.summary.error_message}`}
        description={detail.summary.culprit ? `定责位置：${detail.summary.culprit}` : undefined}
        metrics={[
          { label: "指纹", value: <code>{detail.summary.fingerprint}</code> },
          { label: "累计触发", value: `${detail.summary.count.toLocaleString()} 次` },
          { label: "影响用户", value: `${detail.summary.users.toLocaleString()} 人` },
        ]}
        action={
          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex rounded-md ring-1 ring-border" role="group" aria-label="异常状态操作">
              {(
                [
                  { v: "open", l: "待处理" },
                  { v: "resolved", l: "已解决" },
                  { v: "ignored", l: "忽略" },
                ] as const
              ).map((item) => (
                <Button
                  key={item.v}
                  type="button"
                  size="sm"
                  variant={effectiveStatus === item.v ? "secondary" : "ghost"}
                  className="rounded-none first:rounded-l-md last:rounded-r-md"
                  onClick={() => void handleStatusChange(item.v)}
                >
                  {item.l}
                </Button>
              ))}
            </div>
            <Button
              variant="outline"
              size="sm"
              nativeButton={false}
              render={<Link to={paths.projectSection(projectSlug, "issues", since)} />}
            >
              <ArrowLeft data-icon="inline-start" />
              异常列表
            </Button>
          </div>
        }
      />

      {error ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>刷新异常详情失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      <Card>
        <CardHeader className="border-b border-hairline pb-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <StatePill
                label={
                  effectiveStatus === "open"
                    ? "待处理"
                    : effectiveStatus === "resolved"
                      ? "已解决"
                      : "已忽略"
                }
                tone={
                  effectiveStatus === "open"
                    ? "destructive"
                    : effectiveStatus === "resolved"
                      ? "success"
                      : "outline"
                }
              />
              <Badge variant="outline">
                现场样本 {safeIdx + 1} / {occurrences.length}
              </Badge>
              <span className="font-mono text-muted-foreground tabular-nums">
                {formatLocalTime(currentOcc.ts)} ({formatRelativeTime(currentOcc.ts)})
              </span>
            </div>

            <div className="flex flex-wrap items-center gap-1.5">
              {occurrences.length > 1 ? (
                <>
                  <Button
                    type="button"
                    size="xs"
                    variant="outline"
                    disabled={safeIdx <= 0}
                    onClick={() => updateQuery({ sample: Math.max(0, safeIdx - 1) })}
                  >
                    上一样本
                  </Button>
                  <Button
                    type="button"
                    size="xs"
                    variant="outline"
                    disabled={safeIdx >= occurrences.length - 1}
                    onClick={() => updateQuery({ sample: Math.min(occurrences.length - 1, safeIdx + 1) })}
                  >
                    下一样本
                  </Button>
                </>
              ) : null}

              {currentOcc.session_id ? (
                <Button
                  size="xs"
                  nativeButton={false}
                  render={<Link to={paths.sessionDetail(projectSlug, currentOcc.session_id, since)} />}
                >
                  回放完整会话轨迹
                  <ArrowRight data-icon="inline-end" />
                </Button>
              ) : currentOcc.actor_id ? (
                <Button
                  size="xs"
                  nativeButton={false}
                  render={<Link to={paths.userDetail(projectSlug, currentOcc.actor_id, since)} />}
                >
                  回放用户轨迹
                  <ArrowRight data-icon="inline-end" />
                </Button>
              ) : null}
            </div>
          </div>
        </CardHeader>

        <CardContent className="pt-4">
          <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">触发用户 (Actor)</dt>
              <dd className="truncate font-mono font-medium">
                {currentOcc.actor_id ? (
                  <Link
                    to={paths.userDetail(projectSlug, currentOcc.actor_id, since)}
                    className="text-link hover:underline"
                  >
                    {currentOcc.actor_id}
                  </Link>
                ) : (
                  "匿名"
                )}
              </dd>
            </div>
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">所属会话 (Session)</dt>
              <dd className="truncate font-mono font-medium">
                {currentOcc.session_id ? (
                  <Link
                    to={paths.sessionDetail(projectSlug, currentOcc.session_id, since)}
                    className="text-link hover:underline"
                  >
                    {currentOcc.session_id}
                  </Link>
                ) : (
                  "—"
                )}
              </dd>
            </div>
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">终端环境</dt>
              <dd className="truncate font-medium">
                {currentOcc.browser || "—"} · {currentOcc.os || "—"} ({currentOcc.device_type || "—"})
              </dd>
            </div>
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">版本与环境</dt>
              <dd className="truncate font-mono font-medium">
                {currentOcc.release || "default"} · {currentOcc.env || "production"}
              </dd>
            </div>
            {currentOcc.page_url ? (
              <div className="col-span-2 flex flex-col gap-0.5">
                <dt className="text-muted-foreground">报错页面 URL</dt>
                <dd className="font-mono wrap-anywhere">{currentOcc.page_url}</dd>
              </div>
            ) : null}
            {currentOcc.trace_id ? (
              <div className="col-span-2 flex flex-col gap-0.5">
                <dt className="text-muted-foreground">全链路 Trace ID</dt>
                <dd className="flex items-center gap-2 font-mono">
                  <span className="truncate">{currentOcc.trace_id}</span>
                  <Link
                    to={paths.projectSection(projectSlug, "logs", since, { trace: currentOcc.trace_id })}
                    className="shrink-0 text-link hover:underline"
                  >
                    检索关联日志 →
                  </Link>
                </dd>
              </div>
            ) : null}
          </dl>
        </CardContent>
      </Card>

      <Tabs
        value={activeTab}
        onValueChange={(next) => {
          if (next === "breadcrumbs" || next === "stack" || next === "context") {
            updateQuery({ tab: next });
          }
        }}
      >
        <TabsList>
          <TabsTrigger value="breadcrumbs">
            崩溃前面包屑 ({currentOcc.breadcrumbs.length})
          </TabsTrigger>
          <TabsTrigger value="stack">
            调用堆栈与源码 ({currentOcc.frames.length})
          </TabsTrigger>
          <TabsTrigger value="context">
            Tags 与 Extra ({Object.keys(currentOcc.tags || {}).length})
          </TabsTrigger>
        </TabsList>

        <TabsContent value="breadcrumbs" className="mt-3">
          <BreadcrumbTrail
            projectSlug={projectSlug}
            since={since}
            breadcrumbs={currentOcc.breadcrumbs}
            crashTimestamp={currentOcc.ts}
            filterValue={bcFilter}
            onChangeFilter={setBcFilter}
          />
        </TabsContent>

        <TabsContent value="stack" className="mt-3">
          <StackTraceViewer frames={currentOcc.frames} />
        </TabsContent>

        <TabsContent value="context" className="mt-3">
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-semibold">自定义标签 (Tags)</CardTitle>
              </CardHeader>
              <CardContent>
                {Object.keys(currentOcc.tags || {}).length === 0 ? (
                  <p className="text-xs text-muted-foreground">无自定义标签</p>
                ) : (
                  <div className="flex flex-wrap gap-1.5">
                    {Object.entries(currentOcc.tags).map(([k, v]) => (
                      <span
                        key={k}
                        className="rounded bg-muted px-2 py-1 font-mono text-xs text-foreground"
                      >
                        <strong>{k}</strong>: {v}
                      </span>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-semibold">附加现场数据 (Extra JSON)</CardTitle>
              </CardHeader>
              <CardContent>
                {!currentOcc.extra || Object.keys(currentOcc.extra).length === 0 ? (
                  <p className="text-xs text-muted-foreground">无 Extra 数据</p>
                ) : (
                  <pre className="overflow-x-auto rounded-md bg-muted p-3 font-mono text-xs">
                    {JSON.stringify(currentOcc.extra, null, 2)}
                  </pre>
                )}
              </CardContent>
            </Card>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
