import { useEffect, useState } from "react";
import { apiGet } from "../../apiClient";
import { StatePill } from "../primitives";
import { formatDateTime } from "../../format";
import {
  Table,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription } from "@/components/ui/alert";
import type { Query } from "./types";

export function useObservation<T>(
  view: string,
  app: string,
  token: string,
  query: Query,
  revision = 0,
  enabled = true,
) {
  const params = new URLSearchParams({
    app,
    ...Object.fromEntries(
      Object.entries(query).map(([k, v]) => [k, String(v)]),
    ),
  });
  const key = `${view}?${params}`;
  const [snapshot, setSnapshot] = useState<{
    key: string;
    data?: T;
    loading: boolean;
    error?: string;
  }>({ key, loading: enabled });
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    let disposed = false;
    const timer = window.setTimeout(() => controller.abort(), 15000);
    setSnapshot({ key, loading: true });
    void apiGet<T>(
      `/v1/dashboard/observe/tasks/${key}`,
      token,
      controller.signal,
    )
      .then((data) => {
        if (!disposed) setSnapshot({ key, data, loading: false });
      })
      .catch((failure: unknown) => {
        if (!disposed)
          setSnapshot({
            key,
            loading: false,
            error: controller.signal.aborted
              ? "timeout"
              : /unauthorized|401|403/i.test(String(failure))
                ? "unauthorized"
                : "unreachable",
          });
      })
      .finally(() => window.clearTimeout(timer));
    return () => {
      disposed = true;
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [key, token, revision, enabled]);
  return snapshot.key === key ? snapshot : { key, loading: enabled };
}

const states: Record<string, [string, string]> = {
  timeout: ["观测查询超时", "Observation query timed out"],
  rate_limited: ["观测查询繁忙", "Observation queries are busy"],
  unauthorized: ["观测查询权限已失效", "Observation access has expired"],
  unreachable: ["观测服务暂不可用", "Observation service is unavailable"],
  not_configured: [
    "尚未配置任务观测查询",
    "Task observation queries are not configured",
  ],
  upstream_error: ["观测数据源暂不可用", "Observation source is unavailable"],
  response_limit: [
    "查询结果超过读取上限，请缩小时间范围",
    "Response limit reached; narrow the time range",
  ],
  invalid_response: ["观测数据格式异常", "Unexpected observation response"],
  not_found: [
    "未找到保留的 Trace；可能未采样、已过期或属于其他应用",
    "No retained trace found; it may be unsampled, expired, or outside this application",
  ],
};
export const sourceText = (state: string, zh: boolean) =>
  states[state]?.[zh ? 0 : 1] ||
  (zh ? "观测来源暂不可用" : "Observation source is unavailable");

export function ReadState({
  loading,
  state,
  empty,
  zh,
  retry,
}: {
  loading?: boolean;
  state?: string;
  empty?: string;
  zh: boolean;
  retry?: () => void;
}) {
  if (loading)
    return (
      <div
        className="observe-skeleton"
        role="status"
        aria-label={zh ? "正在查询" : "Loading"}
      >
        {[1, 2, 3, 4].map((i) => (
          <div key={i} />
        ))}
      </div>
    );
  if (state && state !== "ok")
    return (
      <Alert variant="warning">
        <AlertDescription className="flex items-center justify-between gap-3">
          {sourceText(state, zh)}
          {retry && (
            <Button variant="outline" size="sm" onClick={retry}>
              {zh ? "重试" : "Retry"}
            </Button>
          )}
        </AlertDescription>
      </Alert>
    );
  if (empty) return <p className="observe-empty">{empty}</p>;
  return null;
}

const outcomes: Record<string, [string, string]> = {
  succeeded: ["执行成功", "Succeeded"],
  failed: ["执行失败", "Failed"],
  retrying: ["已安排重试", "Retry scheduled"],
  recovered: ["重试后成功", "Recovered"],
  business_failed: ["业务失败", "Business failed"],
  cancelled: ["已取消", "Cancelled"],
  skipped: ["已跳过", "Skipped"],
  unknown: ["结果待确认", "Outcome unknown"],
  ok: ["正常", "OK"],
  error: ["错误", "Error"],
  unset: ["未设置状态", "Status unset"],
};
export const outcomeText = (value: string, zh: boolean) =>
  outcomes[value]?.[zh ? 0 : 1] || value;
export function Outcome({ value, zh }: { value: string; zh: boolean }) {
  const normalized =
    value === "business_failed"
      ? "failed"
      : ["recovered", "ok"].includes(value)
        ? "succeeded"
        : value;
  return <StatePill value={normalized} label={outcomeText(value, zh)} />;
}
export const timeText = (value: number | string | undefined, zh: boolean) => {
  if (!value) return "—";
  return formatDateTime(value, zh ? "zh" : "en");
};
export const durationText = (seconds?: number | null) =>
  seconds == null || !Number.isFinite(seconds)
    ? "—"
    : seconds < 1
      ? `${Math.round(seconds * 1000)} ms`
      : seconds < 60
        ? `${seconds.toFixed(2)} s`
        : `${(seconds / 60).toFixed(1)} min`;

export function AttributeTable({
  values,
}: {
  values: Record<string, unknown>;
}) {
  return (
    <Table className="observe-attributes">
      <TableBody>
        {Object.entries(values).map(([key, value]) => (
          <TableRow key={key}>
            <TableHead scope="row">{key}</TableHead>
            <TableCell>
              {typeof value === "object"
                ? JSON.stringify(value)
                : String(value)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
