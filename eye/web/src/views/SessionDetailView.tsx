import { useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import {
  Activity,
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  Play,
  ScrollText,
  TriangleAlert,
  UserCircle,
  Zap,
} from "@/components/icons";
import { Gauge, Navigation } from "lucide-react";
import { api } from "@/api/client";
import type { TimelineItem } from "@/api/types";
import { BreadcrumbTrail } from "@/components/inspectors/BreadcrumbTrail";
import { StackTraceViewer } from "@/components/inspectors/StackTraceViewer";
import { PageHeader } from "@/components/PageHeader";
import { SessionReplayPlayer, type SeekCommand } from "@/components/replay/SessionReplayPlayer";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { useWorkspace } from "@/context/WorkspaceContext";
import { useQuery } from "@/hooks/useQuery";
import { cn } from "@/lib/utils";
import { paths } from "@/router/paths";
import {
  formatDurationMs,
  formatLocalTime,
  formatOffsetMs,
  formatShortTime,
  shortId,
} from "@/utils/format";
import { getCountryMeta } from "@/utils/geo";

function getTimelineKindMeta(item: TimelineItem) {
  const cls = "size-3.5 shrink-0";
  switch (item.kind) {
    case "pageview":
      return {
        icon: <Navigation className={cls} aria-hidden="true" />,
        label: "页面访问",
        variant: "outline" as const,
      };
    case "custom":
      return {
        icon: <Zap className={cls} aria-hidden="true" />,
        label: "自定义事件",
        variant: "secondary" as const,
      };
    case "identify":
      return {
        icon: <UserCircle className={cls} aria-hidden="true" />,
        label: "身份绑定",
        variant: "secondary" as const,
      };
    case "vital":
      return {
        icon: <Gauge className={cls} aria-hidden="true" />,
        label: "性能指标",
        variant: "outline" as const,
      };
    case "log":
      return {
        icon: <ScrollText className={cls} aria-hidden="true" />,
        label: `日志 · ${item.level || "info"}`,
        variant: item.level === "error" ? ("destructive" as const) : ("outline" as const),
      };
    case "exception":
      return {
        icon: <TriangleAlert className={cn(cls, "text-destructive")} aria-hidden="true" />,
        label: "异常崩溃",
        variant: "destructive" as const,
      };
    default:
      return {
        icon: <Activity className={cls} aria-hidden="true" />,
        label: item.kind,
        variant: "outline" as const,
      };
  }
}

export function SessionDetailView({ mode }: { mode: "session" | "actor" }) {
  const { projectSlug = "", sessionId = "", actorId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const { since, refreshTick } = useWorkspace();

  const kindFilter = searchParams.get("kind") || "all";
  const setKindFilter = (next: string) => {
    const sp = new URLSearchParams(searchParams);
    if (next === "all") sp.delete("kind");
    else sp.set("kind", next);
    setSearchParams(sp, { replace: true });
  };

  const targetId = mode === "session" ? sessionId : actorId;
  const queryKey =
    projectSlug && targetId ? `timeline:${projectSlug}:${mode}:${targetId}:${refreshTick}` : null;

  const { data: timeline, loading, error } = useQuery(queryKey, (signal) =>
    api.timeline(projectSlug, {
      sessionId: mode === "session" ? sessionId : undefined,
      actorId: mode === "actor" ? actorId : undefined,
      since: "7d",
      signal,
    }),
  );

  const [seekCommand, setSeekCommand] = useState<SeekCommand | null>(null);
  const [activeOffsetMs, setActiveOffsetMs] = useState(0);

  const items = timeline?.items || [];
  const filteredItems = useMemo(() => {
    if (kindFilter === "all") return items;
    return items.filter((i) => i.kind === kindFilter);
  }, [items, kindFilter]);

  const activeItemId = useMemo(() => {
    if (filteredItems.length === 0) return null;
    let chosen = filteredItems[0].id;
    for (let i = 0; i < filteredItems.length; i++) {
      if (filteredItems[i].offset_ms <= activeOffsetMs + 80) {
        chosen = filteredItems[i].id;
      } else {
        break;
      }
    }
    return chosen;
  }, [filteredItems, activeOffsetMs]);

  const replaySessionId = useMemo(() => {
    if (mode === "session") {
      return timeline?.session_id || sessionId;
    }
    const withDom = timeline?.user_sessions?.find((s) => s.has_replay);
    if (withDom) return withDom.session_id;
    return timeline?.user_sessions?.[0]?.session_id || items.find((i) => i.session_id)?.session_id || "";
  }, [mode, timeline, sessionId, items]);

  if (loading && !timeline) {
    return (
      <div className="flex flex-col gap-6">
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }

  if (!timeline) {
    return (
      <Empty className="py-12">
        <EmptyHeader>
          <EmptyTitle>{error || "未找到该会话或用户的行为轨迹"}</EmptyTitle>
        </EmptyHeader>
        <Button
          variant="outline"
          size="sm"
          nativeButton={false}
          render={<Link to={paths.projectSection(projectSlug, "sessions", since)} />}
        >
          <ArrowLeft data-icon="inline-start" />
          返回会话列表
        </Button>
      </Empty>
    );
  }

  const country = getCountryMeta(timeline.country);

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader
        compact
        title={
          mode === "actor"
            ? `用户跨会话全量视图 · ${timeline.actor_id}`
            : `会话行为时间轴 · ${timeline.session_id}`
        }
        description="按毫秒级时间顺序还原页面访问、前台停留、自定义业务埋点、性能采样与崩溃前黑匣子面包屑。"
        metrics={[
          { label: "行为步数", value: timeline.total_items.toLocaleString() },
          { label: "总跨度", value: formatDurationMs(timeline.duration_ms) },
          { label: "异常次数", value: timeline.error_count.toLocaleString() },
        ]}
        action={
          <div className="flex flex-wrap items-center gap-2">
            {mode === "session" && timeline.actor_id ? (
              <Button
                variant="outline"
                size="sm"
                nativeButton={false}
                render={<Link to={paths.userDetail(projectSlug, timeline.actor_id, since)} />}
              >
                <UserCircle data-icon="inline-start" />
                该用户全部会话
              </Button>
            ) : null}
            <Button
              variant="outline"
              size="sm"
              nativeButton={false}
              render={<Link to={paths.projectSection(projectSlug, "sessions", since)} />}
            >
              <ArrowLeft data-icon="inline-start" />
              会话列表
            </Button>
          </div>
        }
      />

      {error ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>刷新失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      <SessionReplayPlayer
        projectSlug={projectSlug}
        sessionId={replaySessionId}
        timeline={timeline}
        refreshTick={refreshTick}
        seekCommand={seekCommand}
        onActiveOffsetChange={setActiveOffsetMs}
      />

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm font-semibold">会话上下文属性</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-3 lg:grid-cols-6">
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">用户标识 (Actor)</dt>
              <dd className="truncate font-mono font-medium">{timeline.actor_id || "匿名"}</dd>
            </div>
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">会话标识 (Session)</dt>
              <dd className="truncate font-mono font-medium">{timeline.session_id || "跨会话合并"}</dd>
            </div>
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">国家 / 地区</dt>
              <dd className="font-medium">{country.label}</dd>
            </div>
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">终端环境</dt>
              <dd className="truncate font-medium">
                {timeline.browser || "—"} · {timeline.os || "—"}
              </dd>
            </div>
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">起点时间</dt>
              <dd className="font-mono tabular-nums">{formatLocalTime(timeline.start_ts)}</dd>
            </div>
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">末次动作</dt>
              <dd className="font-mono tabular-nums">{formatLocalTime(timeline.end_ts)}</dd>
            </div>
          </dl>

          {timeline.user_sessions && timeline.user_sessions.length > 1 ? (
            <div className="flex flex-col gap-2 border-t border-hairline pt-3">
              <span className="text-xs text-muted-foreground">
                该用户近 7 天共有 {timeline.user_sessions.length} 次独立会话，点击可切换：
              </span>
              <div className="flex flex-wrap gap-1.5">
                {timeline.user_sessions.map((s, i) => {
                  const active = mode === "session" && s.session_id === timeline.session_id;
                  return (
                    <Button
                      key={s.session_id}
                      size="xs"
                      variant={active ? "secondary" : "outline"}
                      nativeButton={false}
                      render={
                        <Link
                          to={paths.sessionDetail(projectSlug, s.session_id, since)}
                          aria-current={active ? "page" : undefined}
                        />
                      }
                    >
                      <span>
                        #{i + 1} {formatShortTime(s.start_ts)} ({s.total_events} 步)
                      </span>
                      {s.error_count > 0 ? (
                        <Badge variant="destructive">{s.error_count}</Badge>
                      ) : null}
                    </Button>
                  );
                })}
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="border-b border-hairline pb-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <CardTitle>时序行为节点</CardTitle>
              <CardDescription>偏移时间相对于本次轨迹的首个动作计算。</CardDescription>
            </div>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="节点类型过滤">
              {[
                { k: "all", l: `全部 (${items.length})` },
                { k: "pageview", l: `页面 (${items.filter((i) => i.kind === "pageview").length})` },
                { k: "custom", l: `事件 (${items.filter((i) => i.kind === "custom").length})` },
                { k: "vital", l: `性能 (${items.filter((i) => i.kind === "vital").length})` },
                { k: "log", l: `日志 (${items.filter((i) => i.kind === "log").length})` },
                { k: "exception", l: `异常 (${items.filter((i) => i.kind === "exception").length})` },
              ].map((f) => (
                <Button
                  key={f.k}
                  type="button"
                  size="xs"
                  variant={kindFilter === f.k ? "secondary" : "outline"}
                  aria-pressed={kindFilter === f.k}
                  onClick={() => setKindFilter(f.k)}
                >
                  {f.l}
                </Button>
              ))}
            </div>
          </div>
        </CardHeader>

        <CardContent className="px-0 pb-0">
          {filteredItems.length === 0 ? (
            <Empty className="py-10">
              <EmptyHeader>
                <EmptyTitle>当前过滤条件下无行为记录</EmptyTitle>
              </EmptyHeader>
            </Empty>
          ) : (
            <ol className="divide-y divide-hairline">
              {filteredItems.map((item) => {
                const meta = getTimelineKindMeta(item);
                const hasProps = item.props && Object.keys(item.props).length > 0;
                const isCrash = item.kind === "exception";

                const isCurrentStep = activeItemId === item.id;

                return (
                  <li
                    key={item.id}
                    className={cn(
                      "flex flex-col gap-2 px-5 py-3.5 transition-colors",
                      isCrash && "bg-destructive-soft/30",
                      !isCrash && isCurrentStep && "bg-accent/45",
                    )}
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-foreground">
                          {meta.icon}
                          <Badge variant={meta.variant}>{meta.label}</Badge>
                        </span>
                        {item.duration_ms ? (
                          <span className="font-mono text-xs text-muted-foreground tabular-nums">
                            前台停留 {formatDurationMs(item.duration_ms)}
                          </span>
                        ) : null}
                        {item.kind === "vital" && item.metric_value !== undefined ? (
                          <Badge
                            variant={
                              item.metric_rating === "good"
                                ? "success"
                                : item.metric_rating === "poor"
                                  ? "destructive"
                                  : "warning"
                            }
                          >
                            {item.metric_name === "CLS"
                              ? item.metric_value.toFixed(3)
                              : `${Math.round(item.metric_value)}ms`}
                          </Badge>
                        ) : null}
                        {mode === "actor" && item.session_id ? (
                          <Link
                            to={paths.sessionDetail(projectSlug, item.session_id, since)}
                            className="font-mono text-xs text-link hover:underline"
                          >
                            会话 {shortId(item.session_id, 10)} →
                          </Link>
                        ) : null}
                      </div>

                      <div className="flex items-center gap-2 font-mono text-xs text-muted-foreground tabular-nums">
                        <Button
                          type="button"
                          size="xs"
                          variant="ghost"
                          className="h-6 px-2 font-mono text-xs"
                          title="将上方回放器定位到此时间点"
                          onClick={() =>
                            setSeekCommand({
                              offsetMs: item.offset_ms,
                              seq: Date.now(),
                              autoplay: false,
                            })
                          }
                        >
                          <Play data-icon="inline-start" className="size-3" />
                          {formatOffsetMs(item.offset_ms)}
                        </Button>
                        <span>{formatShortTime(item.ts)}</span>
                      </div>
                    </div>

                    <div className="font-mono text-xs text-foreground wrap-anywhere">
                      <strong>{item.title}</strong>
                      {item.subtitle ? <span className="text-muted-foreground"> · {item.subtitle}</span> : null}
                    </div>

                    {hasProps ? (
                      <div className="flex flex-wrap gap-1.5">
                        {Object.entries(item.props!)
                          .slice(0, 8)
                          .map(([k, v]) => (
                            <span
                              key={k}
                              className="rounded bg-muted px-2 py-0.5 font-mono text-[0.6875rem] text-muted-foreground"
                            >
                              {k}: {typeof v === "object" ? JSON.stringify(v) : String(v)}
                            </span>
                          ))}
                      </div>
                    ) : null}

                    {item.trace_id ? (
                      <div>
                        <Link
                          to={paths.projectSection(projectSlug, "logs", since, { trace: item.trace_id })}
                          className="font-mono text-[0.6875rem] text-link hover:underline"
                        >
                          Trace {item.trace_id.slice(0, 16)}… →
                        </Link>
                      </div>
                    ) : null}

                    {isCrash ? (
                      <div className="mt-1 flex flex-col gap-3 rounded-lg border border-destructive/30 bg-card p-3.5">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="text-xs font-medium text-foreground">
                            崩溃前客户端 50 步环形黑匣子面包屑（共 {item.breadcrumbs?.length || 0} 步）
                          </span>
                          {item.fingerprint ? (
                            <Button
                              size="xs"
                              variant="outline"
                              nativeButton={false}
                              render={<Link to={paths.issueDetail(projectSlug, item.fingerprint, { since })} />}
                            >
                              聚合 Issue 详情
                              <ArrowRight data-icon="inline-end" />
                            </Button>
                          ) : null}
                        </div>

                        {item.breadcrumbs && item.breadcrumbs.length > 0 ? (
                          <BreadcrumbTrail
                            projectSlug={projectSlug}
                            since={since}
                            breadcrumbs={item.breadcrumbs}
                            crashTimestamp={item.ts}
                            compact
                          />
                        ) : null}

                        {item.frames && item.frames.length > 0 ? (
                          <details className="text-xs">
                            <summary className="cursor-pointer font-medium text-muted-foreground select-none hover:text-foreground">
                              展开异常调用堆栈 ({item.frames.length} 帧)
                            </summary>
                            <div className="mt-2">
                              <StackTraceViewer frames={item.frames} />
                            </div>
                          </details>
                        ) : null}
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ol>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
