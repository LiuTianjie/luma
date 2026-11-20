import { useEffect, useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { AlertCircle, ArrowRight, Search } from "@/components/icons";
import { api } from "@/api/client";
import type { EventView } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { DataTable, PrimaryCell } from "@/components/primitives";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useWorkspace } from "@/context/WorkspaceContext";
import { useQuery } from "@/hooks/useQuery";
import { paths } from "@/router/paths";
import { formatDurationMs, formatLocalTime, shortId } from "@/utils/format";
import { getCountryMeta } from "@/utils/geo";

const EVENT_TYPES = [
  { value: "all", label: "全部类型" },
  { value: "pageview", label: "页面浏览 (pageview)" },
  { value: "custom", label: "自定义事件 (custom)" },
  { value: "identify", label: "身份绑定 (identify)" },
  { value: "lifecycle", label: "生命周期 (lifecycle)" },
];

export function EventsStreamView() {
  const { projectSlug = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const { since, refreshTick } = useWorkspace();

  const urlType = searchParams.get("type") || "all";
  const urlName = searchParams.get("name") || "";
  const urlQ = searchParams.get("q") || "";

  const [draftName, setDraftName] = useState(urlName);
  const [draftQ, setDraftQ] = useState(urlQ);

  useEffect(() => {
    setDraftName(urlName);
    setDraftQ(urlQ);
  }, [urlName, urlQ]);

  const queryKey = projectSlug
    ? `events:${projectSlug}:${since}:${urlType}:${urlName}:${urlQ}:${refreshTick}`
    : null;

  const { data, loading, error } = useQuery(queryKey, (signal) =>
    api.listEvents(projectSlug, {
      since,
      eventType: urlType,
      eventName: urlName,
      q: urlQ,
      signal,
    }),
  );

  const events = data?.events || [];

  const applyFilters = (patch: { type?: string; name?: string; q?: string }) => {
    const sp = new URLSearchParams(searchParams);
    const nextT = patch.type !== undefined ? patch.type : urlType;
    const nextN = patch.name !== undefined ? patch.name.trim() : urlName;
    const nextQ = patch.q !== undefined ? patch.q.trim() : urlQ;

    if (nextT && nextT !== "all") sp.set("type", nextT);
    else sp.delete("type");

    if (nextN) sp.set("name", nextN);
    else sp.delete("name");

    if (nextQ) sp.set("q", nextQ);
    else sp.delete("q");

    setSearchParams(sp);
  };

  const handleSubmit = (ev: FormEvent) => {
    ev.preventDefault();
    applyFilters({ name: draftName, q: draftQ });
  };

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader
        title="实时事件流"
        description="按时间倒序检视客户端上报的页面访问、前台停留、身份关联及自定义事件 JSON 属性。"
        metrics={[{ label: "匹配事件数", value: events.length.toLocaleString() }]}
      />

      {error ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>事件流加载失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      <Card>
        <CardHeader className="flex flex-col gap-3 border-b border-hairline pb-4">
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="事件类型过滤">
            {EVENT_TYPES.map((opt) => (
              <Button
                key={opt.value}
                type="button"
                size="xs"
                variant={urlType === opt.value ? "secondary" : "outline"}
                aria-pressed={urlType === opt.value}
                onClick={() => applyFilters({ type: opt.value, name: draftName, q: draftQ })}
              >
                {opt.label}
              </Button>
            ))}
          </div>

          <form onSubmit={handleSubmit} className="flex flex-wrap items-center gap-2">
            <Input
              type="text"
              className="w-48 font-mono"
              placeholder="精确事件名 (如 share)"
              value={draftName}
              onChange={(ev) => setDraftName(ev.target.value)}
            />
            <div className="relative min-w-56 flex-1">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                type="search"
                className="pl-8"
                placeholder="全文检索页面路径、URL、Actor ID 或 Props 属性值…"
                value={draftQ}
                onChange={(ev) => setDraftQ(ev.target.value)}
              />
            </div>
            <Button type="submit">检索</Button>
            {urlType !== "all" || urlName || urlQ ? (
              <Button
                type="button"
                variant="ghost"
                onClick={() => {
                  setDraftName("");
                  setDraftQ("");
                  applyFilters({ type: "all", name: "", q: "" });
                }}
              >
                重置
              </Button>
            ) : null}
          </form>
        </CardHeader>

        <CardContent className="px-0 pb-0">
          <DataTable<EventView>
            ariaLabel="实时上报事件流"
            rows={events}
            rowKey={(r, i) => `${r.ts}-${r.session_id}-${i}`}
            defaultSortKey="ts"
            loading={loading}
            emptyTitle="当前筛选条件下暂无事件记录"
            emptyDescription="可调整事件类型或切换右上角时间范围。"
            columns={[
              {
                key: "ts",
                header: "上报时间",
                sortable: true,
                sortValue: (r) => r.ts,
                render: (r) => (
                  <span className="font-mono text-xs text-muted-foreground tabular-nums">
                    {formatLocalTime(r.ts)}
                  </span>
                ),
              },
              {
                key: "name",
                header: "类型 / 事件名",
                sortable: true,
                sortValue: (r) => `${r.event_type}:${r.event_name}`,
                render: (r) => (
                  <div className="flex items-center gap-2">
                    <Badge variant={r.event_type === "custom" ? "secondary" : "outline"}>
                      {r.event_type}
                    </Badge>
                    <strong className="font-mono text-xs font-medium">{r.event_name}</strong>
                  </div>
                ),
              },
              {
                key: "page",
                header: "页面路径与业务属性 (Props)",
                sortable: true,
                sortValue: (r) => r.page_path,
                render: (r) => {
                  const propEntries = Object.entries(r.props || {});
                  return (
                    <div className="flex flex-col gap-1">
                      <div className="font-mono text-xs">
                        <span>{r.page_path || "/"}</span>
                        {r.duration_ms > 0 ? (
                          <span className="text-muted-foreground">
                            {" "}
                            · 停留 {formatDurationMs(r.duration_ms)}
                          </span>
                        ) : null}
                        {r.referrer ? (
                          <span className="text-muted-foreground"> · 来源 {r.referrer}</span>
                        ) : null}
                      </div>
                      {propEntries.length > 0 ? (
                        <div className="flex flex-wrap gap-1">
                          {propEntries.slice(0, 6).map(([k, v]) => (
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
                  );
                },
              },
              {
                key: "env",
                header: "地区 / 终端",
                sortable: true,
                sortValue: (r) => `${r.country}-${r.browser}`,
                render: (r) => {
                  const cm = getCountryMeta(r.country);
                  return <PrimaryCell title={cm.code} meta={`${r.browser || "—"} · ${r.os || "—"}`} />;
                },
              },
              {
                key: "actor",
                header: "关联会话",
                align: "right",
                render: (r) =>
                  r.session_id ? (
                    <Button
                      size="xs"
                      variant="outline"
                      nativeButton={false}
                      render={<Link to={paths.sessionDetail(projectSlug, r.session_id, since)} />}
                    >
                      <span className="font-mono">{shortId(r.actor_id || r.session_id, 10)}</span>
                      <ArrowRight data-icon="inline-end" />
                    </Button>
                  ) : (
                    <span className="text-xs text-subtle-foreground">—</span>
                  ),
              },
            ]}
          />
        </CardContent>
      </Card>
    </div>
  );
}
