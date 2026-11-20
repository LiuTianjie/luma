import { useEffect, useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { AlertCircle, ArrowRight, Info, Search } from "@/components/icons";
import { api } from "@/api/client";
import type { LogView } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { DataTable, SelectControl, StatePill } from "@/components/primitives";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useWorkspace } from "@/context/WorkspaceContext";
import { useQuery } from "@/hooks/useQuery";
import { paths } from "@/router/paths";
import { formatLocalTime, shortId } from "@/utils/format";

const LEVEL_OPTIONS = [
  { value: "all", label: "全部级别" },
  { value: "error", label: "ERROR" },
  { value: "warn", label: "WARN" },
  { value: "info", label: "INFO" },
  { value: "debug", label: "DEBUG" },
];

export function LogsView() {
  const { projectSlug = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const { since, refreshTick } = useWorkspace();

  const urlLevel = searchParams.get("level") || "all";
  const urlQ = searchParams.get("q") || "";
  const urlTrace = searchParams.get("trace") || "";

  const [draftQ, setDraftQ] = useState(urlQ);
  const [draftTrace, setDraftTrace] = useState(urlTrace);

  useEffect(() => {
    setDraftQ(urlQ);
    setDraftTrace(urlTrace);
  }, [urlQ, urlTrace]);

  const queryKey = projectSlug
    ? `logs:${projectSlug}:${since}:${urlLevel}:${urlQ}:${urlTrace}:${refreshTick}`
    : null;

  const { data, loading, error } = useQuery(queryKey, (signal) =>
    api.searchLogs(projectSlug, {
      level: urlLevel,
      q: urlQ,
      traceId: urlTrace,
      since,
      signal,
    }),
  );

  const logs = data?.logs || [];

  const applyFilters = (patch: { level?: string; q?: string; trace?: string }) => {
    const sp = new URLSearchParams(searchParams);
    const nextLvl = patch.level !== undefined ? patch.level : urlLevel;
    const nextQ = patch.q !== undefined ? patch.q.trim() : urlQ;
    const nextT = patch.trace !== undefined ? patch.trace.trim() : urlTrace;

    if (nextLvl && nextLvl !== "all") sp.set("level", nextLvl);
    else sp.delete("level");

    if (nextQ) sp.set("q", nextQ);
    else sp.delete("q");

    if (nextT) sp.set("trace", nextT);
    else sp.delete("trace");

    setSearchParams(sp);
  };

  const handleSubmit = (ev: FormEvent) => {
    ev.preventDefault();
    applyFilters({ q: draftQ, trace: draftTrace });
  };

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader
        title="客户端日志"
        description="支持 Token 倒排索引搜索客户端常态化上报的结构化日志，并通过 W3C Trace ID 与会话轨迹串联。"
        metrics={[{ label: "匹配日志数", value: logs.length.toLocaleString() }]}
      />

      <Alert>
        <Info />
        <AlertTitle>客户端日志采集策略</AlertTitle>
        <AlertDescription>
          默认情况下，SDK 将 <code>console.*</code> 缓存在客户端内存 50 步环形黑匣子中（报错崩溃时自动随黑匣子带出）。如需常态化落盘检索，请在初始化时开启{" "}
          <code>captureConsoleLogs: true</code> 或调用 <code>eye.log(level, message, attrs)</code>。
        </AlertDescription>
      </Alert>

      {error ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>日志查询失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      <Card>
        <CardHeader className="border-b border-hairline pb-4">
          <form onSubmit={handleSubmit} className="flex flex-wrap items-center gap-2">
            <SelectControl
              ariaLabel="日志级别"
              value={urlLevel}
              options={LEVEL_OPTIONS}
              onChange={(next) => applyFilters({ level: next, q: draftQ, trace: draftTrace })}
              className="w-32"
            />

            <div className="relative min-w-56 flex-1">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                type="search"
                className="pl-8"
                placeholder="关键词搜索 message 或结构化 attrs…"
                value={draftQ}
                onChange={(ev) => setDraftQ(ev.target.value)}
              />
            </div>

            <Input
              type="text"
              className="w-52 font-mono"
              placeholder="Trace ID 过滤"
              value={draftTrace}
              onChange={(ev) => setDraftTrace(ev.target.value)}
            />

            <Button type="submit">检索</Button>

            {urlLevel !== "all" || urlQ || urlTrace ? (
              <Button
                type="button"
                variant="ghost"
                onClick={() => {
                  setDraftQ("");
                  setDraftTrace("");
                  applyFilters({ level: "all", q: "", trace: "" });
                }}
              >
                重置
              </Button>
            ) : null}
          </form>
        </CardHeader>

        <CardContent className="px-0 pb-0">
          <DataTable<LogView>
            ariaLabel="客户端日志列表"
            rows={logs}
            rowKey={(r, i) => `${r.ts}-${r.trace_id}-${i}`}
            defaultSortKey="ts"
            loading={loading}
            emptyTitle="当前时间窗口内暂无落盘日志"
            emptyDescription={
              <span>
                页面浏览与交互埋点请前往{" "}
                <Link to={paths.projectSection(projectSlug, "events", since)} className="text-link hover:underline">
                  实时事件流
                </Link>{" "}
                或{" "}
                <Link to={paths.projectSection(projectSlug, "sessions", since)} className="text-link hover:underline">
                  用户会话与轨迹
                </Link>{" "}
                查看。
              </span>
            }
            columns={[
              {
                key: "ts",
                header: "时间",
                sortable: true,
                sortValue: (r) => r.ts,
                render: (r) => (
                  <span className="font-mono text-xs text-muted-foreground tabular-nums">
                    {formatLocalTime(r.ts)}
                  </span>
                ),
              },
              {
                key: "level",
                header: "级别",
                sortable: true,
                sortValue: (r) => r.level,
                render: (r) => (
                  <StatePill
                    label={r.level.toUpperCase()}
                    tone={
                      r.level === "error"
                        ? "destructive"
                        : r.level === "warn"
                          ? "warning"
                          : "outline"
                    }
                  />
                ),
              },
              {
                key: "msg",
                header: "消息与结构化字段",
                sortable: true,
                sortValue: (r) => r.message,
                render: (r) => (
                  <div className="flex flex-col gap-1">
                    <div className="font-mono text-xs wrap-anywhere">{r.message}</div>
                    {r.page_url ? (
                      <div className="font-mono text-[0.6875rem] text-muted-foreground wrap-anywhere">
                        {r.page_url}
                      </div>
                    ) : null}
                    {r.attrs && Object.keys(r.attrs).length > 0 ? (
                      <div className="flex flex-wrap gap-1">
                        {Object.entries(r.attrs).map(([k, v]) => (
                          <span
                            key={k}
                            className="rounded bg-muted px-1.5 py-0.5 font-mono text-[0.6875rem] text-muted-foreground"
                          >
                            {k}: {typeof v === "object" ? JSON.stringify(v) : String(v)}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ),
              },
              {
                key: "trace",
                header: "Trace / 会话",
                align: "right",
                render: (r) => (
                  <div className="flex flex-wrap justify-end gap-1.5">
                    {r.trace_id ? (
                      <Button
                        type="button"
                        size="xs"
                        variant="ghost"
                        onClick={() => applyFilters({ trace: r.trace_id })}
                        className="font-mono"
                      >
                        {r.trace_id.slice(0, 10)}…
                      </Button>
                    ) : null}
                    {r.session_id ? (
                      <Button
                        size="xs"
                        variant="outline"
                        nativeButton={false}
                        render={<Link to={paths.sessionDetail(projectSlug, r.session_id, since)} />}
                      >
                        <span className="font-mono">{shortId(r.actor_id || r.session_id, 10)}</span>
                        <ArrowRight data-icon="inline-end" />
                      </Button>
                    ) : null}
                  </div>
                ),
              },
            ]}
          />
        </CardContent>
      </Card>
    </div>
  );
}
