import { useEffect, useState } from "react";
import { apiGet } from "../apiClient";
import { investigationPath } from "../observeContext";
import { toHref, useRouter } from "../router";
import { formatDateTime } from "../format";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
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
    { search } = useRouter(),
    [revision, setRevision] = useState(0),
    [result, setResult] = useState<{
      state: string;
      items: AlertRow[];
      truncated?: boolean;
    }>();
  useEffect(() => {
    const c = new AbortController();
    let disposed = false;
    const t = setTimeout(() => c.abort(), 5000);
    setResult(undefined);
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
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>
            {zh ? "观测规则告警" : "Observation rule alerts"}{" "}
            <Badge variant="outline">Alertmanager</Badge>
          </CardTitle>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setRevision((n) => n + 1)}
          >
            {zh ? "刷新" : "Refresh"}
          </Button>
        </div>
        <CardDescription>
          {zh
            ? "与 Control 告警集中展示；此来源的静默与通知由 Alertmanager 管理，不会再次发送通知。"
            : "Shown alongside Control incidents. Alertmanager owns silences and notifications; this view does not resend them."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {!result ? (
          <p role="status" className="text-sm text-muted-foreground">
            {zh ? "正在读取…" : "Loading…"}
          </p>
        ) : result.state !== "ok" ? (
          <p role="status" className="text-sm text-muted-foreground">
            {zh
              ? "Alertmanager 暂不可用，不能据此判断没有告警。"
              : "Alertmanager is unavailable; alert state is unknown."}
          </p>
        ) : !result.items.length ? (
          <p className="text-sm text-muted-foreground">
            {zh
              ? "此来源当前没有告警。"
              : "No current alerts from this source."}
          </p>
        ) : (
          result.items.map((row) => (
            <details key={row.id} className="rounded-lg border p-3">
              <summary className="cursor-pointer text-sm font-medium">
                {row.summary || row.name}
                <span className="ml-3 font-normal text-muted-foreground">
                  {row.app} {row.task} · {row.status}
                </span>
              </summary>
              <div className="mt-3 space-y-2 text-sm">
                <p className="whitespace-pre-wrap break-words">
                  {row.description}
                </p>
                <p className="text-muted-foreground">
                  {zh ? "最近更新" : "Last update"} ·{" "}
                  {formatDateTime(row.updatedAt, lang)}
                </p>
                {row.app && (
                  <a
                    className="text-link hover:underline"
                    href={toHref(
                      investigationPath(
                        `/apps/${encodeURIComponent(row.app)}/logs`,
                        search,
                        {
                          observe_since: String(
                            Math.max(
                              Math.floor(Date.now() / 1000) - 259200,
                              (Math.floor(
                                new Date(row.startsAt).getTime() / 1000,
                              ) || Math.floor(Date.now() / 1000)) - 300,
                            ),
                          ),
                          observe_until: String(Math.floor(Date.now() / 1000)),
                        },
                      ),
                    )}
                  >
                    {zh ? "查看相关日志" : "Related logs"}
                  </a>
                )}
              </div>
            </details>
          ))
        )}
        {result?.truncated && (
          <p className="text-sm text-muted-foreground">
            {zh
              ? "仅显示前 200 条告警，请按应用缩小范围。"
              : "Showing the first 200 alerts. Narrow by application."}
          </p>
        )}
      </CardContent>
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
