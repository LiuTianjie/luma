import { useEffect, useState } from "react";
import { apiGet } from "../apiClient";
import { investigationPath } from "../observeContext";
import { spaLink, useRouter } from "../router";
import { cn } from "@/lib/utils";
import { useObserveRefresh, useRefreshEvent } from "./observeRefresh";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "./ui/collapsible";
import { Skeleton } from "./ui/skeleton";
import { ArrowRight, CheckCircle2, ChevronRightIcon, RefreshCw, TriangleAlert } from "@/components/icons";
import { formatDateTime } from "../format";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  CardAction,
  CardFooter,
} from "./ui/card";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import type { Lang } from "../types";
type AlertRow = {
  id: string;
  app: string;
  task: string;
  name: string;
  summary: string;
  description: string;
  startsAt: string;
  updatedAt: string;
  status: string;
};
function statusLabel(status: string, zh: boolean) {
  if (status === "active" || status === "firing") return zh ? "触发中" : "Firing";
  if (status === "suppressed") return zh ? "已静默" : "Silenced";
  if (status === "unprocessed") return zh ? "待处理" : "Unprocessed";
  return status;
}

export function ExternalAlerts({
  app = "",
  lang,
  token,
}: {
  app?: string;
  lang: Lang;
  token: string;
}) {
  const zh = lang === "zh",
    { search, navigate } = useRouter(),
    refreshHost = useObserveRefresh(),
    [revision, setRevision] = useState(0),
    [result, setResult] = useState<{
      state: string;
      items: AlertRow[];
      truncated?: boolean;
    }>();
  useRefreshEvent(() => setRevision((n) => n + 1));
  useEffect(() => {
    const c = new AbortController();
    let disposed = false;
    const t = setTimeout(() => c.abort(), 5000);
    // Keep the previous rows while refreshing so the list does not flash empty.
    void apiGet<{ state: string; items: AlertRow[]; truncated?: boolean }>(
      `/v1/dashboard/observe/alerts?${new URLSearchParams({ app })}`,
      token,
      c.signal,
    )
      .then((data) => {
        if (!disposed) setResult(data);
      })
      .catch(() => {
        if (!disposed) setResult({ state: "unavailable", items: [] });
      })
      .finally(() => clearTimeout(t));
    return () => {
      disposed = true;
      c.abort();
      clearTimeout(t);
    };
  }, [app, token, revision]);
  const logsHref = (row: AlertRow) =>
    investigationPath(`/apps/${encodeURIComponent(row.app)}/logs`, search, {
      observe_since: String(
        Math.max(
          Math.floor(Date.now() / 1000) - 259200,
          (Math.floor(new Date(row.startsAt).getTime() / 1000) || Math.floor(Date.now() / 1000)) - 300,
        ),
      ),
      observe_until: String(Math.floor(Date.now() / 1000)),
    });
  return (
    <Card className="has-[>ul:last-child]:pb-0">
      <CardHeader className={cn(result?.state === "ok" && result.items.length ? "border-b border-hairline" : "")}>
        <CardTitle className="flex flex-wrap items-center gap-2">
          {zh ? "观测规则告警" : "Observation rule alerts"}
          <Badge variant="secondary">Alertmanager</Badge>
          {result?.state === "ok" && result.items.length ? <span className="text-[0.8125rem] font-normal text-muted-foreground tabular-nums">{result.items.length}</span> : null}
        </CardTitle>
        <CardDescription>
          {zh
            ? "由 vmalert 规则产生，静默与通知在 Alertmanager 中管理，Luma 不会重复发送。"
            : "Raised by vmalert rules. Alertmanager owns silences and notifications; Luma does not resend them."}
        </CardDescription>
        {!refreshHost.hosted && (
          <CardAction>
            <Button size="sm" variant="outline" onClick={() => setRevision((n) => n + 1)}>
              <RefreshCw data-icon="inline-start" aria-hidden="true" />
              {zh ? "刷新" : "Refresh"}
            </Button>
          </CardAction>
        )}
      </CardHeader>
      {!result ? (
        <CardContent aria-busy="true">
          <span role="status" className="sr-only">{zh ? "正在读取…" : "Loading…"}</span>
          <Skeleton className="h-9 w-full" />
        </CardContent>
      ) : result.state !== "ok" ? (
        <CardContent>
          <p role="status" className="flex items-center gap-2 text-[0.8125rem] text-warning-foreground">
            <TriangleAlert className="size-4 shrink-0" aria-hidden="true" />
            {zh
              ? "Alertmanager 暂不可用，不能据此判断没有告警。"
              : "Alertmanager is unavailable; alert state is unknown."}
          </p>
        </CardContent>
      ) : !result.items.length ? (
        <CardContent>
          <p className="flex items-center gap-2 text-[0.8125rem] text-muted-foreground">
            <CheckCircle2 className="size-4 shrink-0 text-success" aria-hidden="true" />
            {zh ? "此来源当前没有告警。" : "No current alerts from this source."}
          </p>
        </CardContent>
      ) : (
        <ul className="-mt-(--card-spacing) divide-y divide-hairline" aria-label={zh ? "观测规则告警" : "Observation rule alerts"}>
          {result.items.map((row) => (
            <li key={row.id}>
              <Collapsible>
                <CollapsibleTrigger className="group/row flex w-full items-center gap-3 px-(--card-spacing) py-3 text-left outline-none hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:ring-inset">
                  <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-data-[panel-open]/row:rotate-90" aria-hidden="true" />
                  <span aria-hidden="true" className={cn("size-2 shrink-0 rounded-full", row.status === "suppressed" ? "bg-subtle-foreground" : "bg-destructive")} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{row.summary || row.name}</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {[row.app, row.task].filter(Boolean).join(" / ") || row.name}
                    </span>
                  </span>
                  <Badge variant={row.status === "suppressed" ? "secondary" : "destructive"}>{statusLabel(row.status, zh)}</Badge>
                  <span className="hidden w-40 shrink-0 text-right text-xs text-muted-foreground tabular-nums md:block">{formatDateTime(row.startsAt, lang)}</span>
                </CollapsibleTrigger>
                <CollapsibleContent className="flex flex-col gap-3 border-t border-hairline bg-muted/30 px-(--card-spacing) py-3 pl-14 text-[0.8125rem]">
                  {row.description ? <p className="whitespace-pre-wrap wrap-anywhere">{row.description}</p> : null}
                  <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-xs">
                    <dt className="text-muted-foreground">{zh ? "规则" : "Rule"}</dt>
                    <dd className="font-mono" translate="no">{row.name}</dd>
                    <dt className="text-muted-foreground">{zh ? "开始" : "Started"}</dt>
                    <dd className="tabular-nums">{formatDateTime(row.startsAt, lang)}</dd>
                    <dt className="text-muted-foreground">{zh ? "最近更新" : "Last update"}</dt>
                    <dd className="tabular-nums">{formatDateTime(row.updatedAt, lang)}</dd>
                  </dl>
                  {row.app && (
                    <a className="inline-flex w-fit items-center gap-1 text-link underline-offset-4 hover:underline" {...spaLink(logsHref(row), navigate)}>
                      {zh ? "查看相关日志" : "Related logs"}
                      <ArrowRight className="size-3.5" aria-hidden="true" />
                    </a>
                  )}
                </CollapsibleContent>
              </Collapsible>
            </li>
          ))}
        </ul>
      )}
      {result?.truncated && (
        <CardFooter className="text-xs text-muted-foreground">
          {zh
            ? "仅显示前 200 条告警，请按应用缩小范围。"
            : "Showing the first 200 alerts. Narrow by application."}
        </CardFooter>
      )}
    </Card>
  );
}
export function evidenceSummary(raw: string): string {
  try {
    const d = JSON.parse(raw);
    const exception = d.exception || {};
    return String(
      exception.message ||
        d.error_message ||
        d.message ||
        d.event_type ||
        d.event ||
        raw,
    ).slice(0, 300);
  } catch {
    return raw.slice(0, 300);
  }
}
