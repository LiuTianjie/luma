import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { AlertCircle, ArrowRight, Play } from "@/components/icons";
import { api } from "@/api/client";
import type { IssueSummary } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { DataTable, PrimaryCell, StatePill } from "@/components/primitives";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { useWorkspace } from "@/context/WorkspaceContext";
import { useQuery } from "@/hooks/useQuery";
import { paths } from "@/router/paths";
import { formatLocalTime, formatRelativeTime } from "@/utils/format";

const STATUS_OPTIONS = [
  { value: "open", label: "待处理 (Open)" },
  { value: "resolved", label: "已解决 (Resolved)" },
  { value: "ignored", label: "已忽略 (Ignored)" },
  { value: "all", label: "全部状态" },
];

export function IssuesView() {
  const { projectSlug = "" } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { projects, since, refreshTick, triggerRefresh, notify } = useWorkspace();

  const statusFilter = searchParams.get("status") || "open";
  const [sendingDrill, setSendingDrill] = useState(false);

  const setStatusFilter = (next: string) => {
    const sp = new URLSearchParams(searchParams);
    if (next === "open") sp.delete("status");
    else sp.set("status", next);
    setSearchParams(sp);
  };

  const queryKey = projectSlug
    ? `issues:${projectSlug}:${since}:${statusFilter}:${refreshTick}`
    : null;

  const { data, loading, error } = useQuery(queryKey, (signal) =>
    api.listIssues(projectSlug, since, statusFilter, signal),
  );

  const issues = data?.issues || [];

  const handleSendCrashDrill = async () => {
    const proj = projects.find((p) => p.slug === projectSlug);
    if (!proj) return;
    setSendingDrill(true);
    try {
      const { sessionId } = await api.sendDiagnosticSample(proj, "crash");
      await new Promise((r) => setTimeout(r, 1200));
      triggerRefresh();
      notify(`已写入演练崩溃样本及 8 步操作面包屑（会话 ${sessionId}）。`);
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setSendingDrill(false);
    }
  };

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader
        title="异常与黑匣子"
        description="按调用栈指纹去重聚合未捕获 JS 异常与崩溃。点击任意记录查看报错前 50 步用户点击、网络请求与 SourceMap 源码行。"
        metrics={[{ label: "异常聚合组", value: issues.length.toLocaleString() }]}
        action={
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={sendingDrill}
            onClick={() => void handleSendCrashDrill()}
          >
            {sendingDrill ? (
              <Spinner aria-hidden="true" data-icon="inline-start" />
            ) : (
              <Play data-icon="inline-start" />
            )}
            发送黑匣子演练样本
          </Button>
        }
      />

      {error ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>异常列表加载失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      <Card>
        <CardHeader className="border-b border-hairline pb-4">
          <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="异常状态过滤">
            {STATUS_OPTIONS.map((opt) => (
              <Button
                key={opt.value}
                type="button"
                size="xs"
                variant={statusFilter === opt.value ? "secondary" : "outline"}
                aria-pressed={statusFilter === opt.value}
                onClick={() => setStatusFilter(opt.value)}
              >
                {opt.label}
              </Button>
            ))}
          </div>
        </CardHeader>

        <CardContent className="px-0 pb-0">
          <DataTable<IssueSummary>
            ariaLabel="异常指纹聚合列表"
            rows={issues}
            rowKey={(r) => r.fingerprint}
            defaultSortKey="last_seen"
            loading={loading}
            onRowClick={(r) => navigate(paths.issueDetail(projectSlug, r.fingerprint, { since }))}
            emptyTitle="当前筛选窗口内无匹配异常"
            emptyDescription="线上运行正常。客户端内存中的 50 步环形黑匣子仅在发生异常时自动随错误堆栈一并上报。"
            columns={[
              {
                key: "title",
                header: "异常类型与摘要",
                sortable: true,
                sortValue: (r) => `${r.error_type}: ${r.error_message}`,
                render: (r) => (
                  <Link
                    to={paths.issueDetail(projectSlug, r.fingerprint, { since })}
                    className="block max-w-xl hover:underline"
                  >
                    <PrimaryCell
                      title={
                        <span>
                          <span className="font-mono text-destructive-foreground">{r.error_type}</span>
                          <span className="text-muted-foreground">: </span>
                          <span>{r.error_message || "(无错误消息)"}</span>
                        </span>
                      }
                      meta={
                        <span className="font-mono">
                          {r.culprit || "unknown"} · {r.fingerprint}
                          {r.last_release ? ` · ${r.last_release}` : ""}
                        </span>
                      }
                    />
                  </Link>
                ),
              },
              {
                key: "status",
                header: "状态",
                sortable: true,
                sortValue: (r) => r.status,
                render: (r) =>
                  r.status === "open" ? (
                    <StatePill label="待处理" tone="destructive" />
                  ) : r.status === "resolved" ? (
                    <StatePill label="已解决" tone="success" />
                  ) : (
                    <StatePill label="已忽略" tone="outline" />
                  ),
              },
              {
                key: "count",
                header: "触发次数",
                align: "right",
                sortable: true,
                sortValue: (r) => r.count,
                render: (r) => <strong className="font-mono tabular-nums">{r.count.toLocaleString()}</strong>,
              },
              {
                key: "users",
                header: "影响用户",
                align: "right",
                sortable: true,
                sortValue: (r) => r.users,
                render: (r) => <span className="font-mono tabular-nums">{r.users.toLocaleString()}</span>,
              },
              {
                key: "last_seen",
                header: "最近发生",
                align: "right",
                sortable: true,
                sortValue: (r) => r.last_seen,
                render: (r) => (
                  <div className="flex flex-col items-end gap-0.5">
                    <span className="text-xs">{formatRelativeTime(r.last_seen)}</span>
                    <span className="font-mono text-[0.6875rem] text-muted-foreground tabular-nums">
                      {formatLocalTime(r.last_seen)}
                    </span>
                  </div>
                ),
              },
              {
                key: "act",
                header: "",
                align: "right",
                render: (r) => (
                  <Button
                    size="xs"
                    variant="outline"
                    nativeButton={false}
                    render={<Link to={paths.issueDetail(projectSlug, r.fingerprint, { since })} />}
                  >
                    现场诊断
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
