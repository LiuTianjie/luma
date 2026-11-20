import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { AlertCircle, ArrowRight, Play, Search } from "@/components/icons";
import { api } from "@/api/client";
import type { SessionSummary } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { DataTable, PrimaryCell, StatePill } from "@/components/primitives";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useWorkspace } from "@/context/WorkspaceContext";
import { useQuery } from "@/hooks/useQuery";
import { paths } from "@/router/paths";
import { formatDurationSec, formatLocalTime, formatRelativeTime, shortId } from "@/utils/format";
import { getCountryMeta } from "@/utils/geo";

export function SessionsView() {
  const { projectSlug = "" } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { since, refreshTick } = useWorkspace();

  const urlQ = searchParams.get("q") || "";
  const urlCountry = searchParams.get("country") || "";
  const urlErrorsOnly = searchParams.get("errors") === "1";

  const [draftQ, setDraftQ] = useState(urlQ);
  const [draftCountry, setDraftCountry] = useState(urlCountry);

  useEffect(() => {
    setDraftQ(urlQ);
    setDraftCountry(urlCountry);
  }, [urlQ, urlCountry]);

  const queryKey = projectSlug
    ? `sessions:${projectSlug}:${since}:${urlQ}:${urlCountry}:${urlErrorsOnly ? 1 : 0}:${refreshTick}`
    : null;

  const { data, loading, error } = useQuery(queryKey, (signal) =>
    api.listSessions(projectSlug, {
      since,
      q: urlQ,
      country: urlCountry,
      hasError: urlErrorsOnly,
      signal,
    }),
  );

  const sessions = data?.sessions || [];

  const applyFilters = (patch: { q?: string; country?: string; errors?: boolean }) => {
    const sp = new URLSearchParams(searchParams);
    const nextQ = patch.q !== undefined ? patch.q.trim() : urlQ;
    const nextC = patch.country !== undefined ? patch.country.trim().toUpperCase() : urlCountry;
    const nextE = patch.errors !== undefined ? patch.errors : urlErrorsOnly;

    if (nextQ) sp.set("q", nextQ);
    else sp.delete("q");

    if (nextC) sp.set("country", nextC);
    else sp.delete("country");

    if (nextE) sp.set("errors", "1");
    else sp.delete("errors");

    setSearchParams(sp);
  };

  const handleSubmit = (ev: FormEvent) => {
    ev.preventDefault();
    applyFilters({ q: draftQ, country: draftCountry });
  };

  const hasActiveFilter = Boolean(urlQ || urlCountry || urlErrorsOnly);

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader
        title="用户会话与轨迹"
        description="基于 30 分钟空闲滑动窗口聚合真实访问会话。点击任意会话可按时间轴回放其完整浏览、点击与报错过程。"
        metrics={[
          { label: "返回会话数", value: sessions.length.toLocaleString() },
          ...(urlCountry ? [{ label: "国家过滤", value: urlCountry }] : []),
        ]}
      />

      {error ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>会话查询失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      <Card>
        <CardHeader className="border-b border-hairline pb-4">
          <form onSubmit={handleSubmit} className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-56 flex-1">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                type="search"
                className="pl-8"
                placeholder="搜索 Actor ID、Session ID、页面路径、浏览器或系统…"
                value={draftQ}
                onChange={(ev) => setDraftQ(ev.target.value)}
              />
            </div>

            <Input
              type="text"
              className="w-28 font-mono uppercase"
              placeholder="国家码 CN"
              maxLength={4}
              value={draftCountry}
              onChange={(ev) => setDraftCountry(ev.target.value.toUpperCase())}
            />

            <Button
              type="button"
              variant={urlErrorsOnly ? "secondary" : "outline"}
              aria-pressed={urlErrorsOnly}
              onClick={() => applyFilters({ q: draftQ, country: draftCountry, errors: !urlErrorsOnly })}
            >
              仅含异常会话
            </Button>

            <Button type="submit">筛选</Button>

            {hasActiveFilter ? (
              <Button
                type="button"
                variant="ghost"
                onClick={() => {
                  setDraftQ("");
                  setDraftCountry("");
                  applyFilters({ q: "", country: "", errors: false });
                }}
              >
                重置
              </Button>
            ) : null}
          </form>
        </CardHeader>

        <CardContent className="px-0 pb-0">
          <DataTable<SessionSummary>
            ariaLabel="用户会话列表"
            rows={sessions}
            rowKey={(r) => r.session_id}
            defaultSortKey="last_ts"
            loading={loading}
            onRowClick={(r) => navigate(paths.sessionDetail(projectSlug, r.session_id, since))}
            emptyTitle="当前筛选窗口内未找到匹配会话"
            emptyDescription="可尝试切换右上角时间窗口至近 7 天，或重置上方的筛选条件。"
            columns={[
              {
                key: "actor",
                header: "用户 / 会话标识",
                sortable: true,
                sortValue: (r) => r.actor_id || r.session_id,
                render: (r) => (
                  <PrimaryCell
                    title={
                      r.actor_id ? (
                        <Link
                          to={paths.userDetail(projectSlug, r.actor_id, since)}
                          className="font-mono hover:underline"
                        >
                          {shortId(r.actor_id, 18)}
                        </Link>
                      ) : (
                        "匿名访客"
                      )
                    }
                    meta={
                      <Link
                        to={paths.sessionDetail(projectSlug, r.session_id, since)}
                        className="font-mono hover:underline"
                      >
                        {shortId(r.session_id, 18)}
                      </Link>
                    }
                  />
                ),
              },
              {
                key: "geo",
                header: "国家 / 终端环境",
                sortable: true,
                sortValue: (r) => `${r.country}-${r.os}-${r.browser}`,
                render: (r) => {
                  const cm = getCountryMeta(r.country);
                  return (
                    <PrimaryCell
                      title={cm.label}
                      meta={`${r.browser || "—"} · ${r.os || "—"} (${r.device_type || "—"})`}
                    />
                  );
                },
              },
              {
                key: "journey",
                header: "访问路径（首屏 → 末屏）",
                sortable: true,
                sortValue: (r) => r.entry_page || "",
                render: (r) => (
                  <div className="max-w-xs truncate font-mono text-xs" title={`${r.entry_page || "/"} -> ${r.exit_page || "/"}`}>
                    <span>{r.entry_page || "/"}</span>
                    {r.exit_page && r.exit_page !== r.entry_page ? (
                      <span className="text-muted-foreground"> → {r.exit_page}</span>
                    ) : null}
                  </div>
                ),
              },
              {
                key: "steps",
                header: "浏览 / 事件",
                align: "right",
                sortable: true,
                sortValue: (r) => r.total_events,
                render: (r) => (
                  <span className="font-mono text-xs tabular-nums">
                    {r.pageviews} PV · {r.custom_events} 事件
                  </span>
                ),
              },
              {
                key: "dur",
                header: "持续时长",
                align: "right",
                sortable: true,
                sortValue: (r) => r.duration_sec,
                render: (r) => (
                  <span className="font-mono text-xs tabular-nums">{formatDurationSec(r.duration_sec)}</span>
                ),
              },
              {
                key: "status",
                header: "状态 / 回放",
                align: "right",
                sortable: true,
                sortValue: (r) => r.error_count * 10 + (r.has_replay ? 1 : 0),
                render: (r) => (
                  <div className="inline-flex items-center justify-end gap-1.5">
                    {r.has_replay ? <Badge variant="info">1:1 画面</Badge> : null}
                    {r.error_count > 0 ? (
                      <StatePill label={`${r.error_count} 异常`} tone="destructive" />
                    ) : (
                      <StatePill label="正常" tone="success" />
                    )}
                  </div>
                ),
              },
              {
                key: "last_ts",
                header: "最近活跃",
                align: "right",
                sortable: true,
                sortValue: (r) => r.last_ts,
                render: (r) => (
                  <div className="flex flex-col items-end gap-0.5">
                    <span className="text-xs">{formatRelativeTime(r.last_ts)}</span>
                    <span className="font-mono text-[0.6875rem] text-muted-foreground tabular-nums">
                      {formatLocalTime(r.last_ts)}
                    </span>
                  </div>
                ),
              },
              {
                key: "action",
                header: "",
                align: "right",
                render: (r) => (
                  <Button
                    size="xs"
                    variant={r.has_replay ? "secondary" : "outline"}
                    nativeButton={false}
                    render={<Link to={paths.sessionDetail(projectSlug, r.session_id, since)} />}
                  >
                    <Play data-icon="inline-start" className="size-3" />
                    {r.has_replay ? "观看回放" : "回放轨迹"}
                    <ArrowRight data-icon="inline-end" />
                  </Button>
                ),
              },
            ]}
          />
        </CardContent>
      </Card>
    </div>
  );
}
