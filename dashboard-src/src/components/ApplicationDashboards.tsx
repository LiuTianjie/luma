import { useEffect, useState } from "react";
import { grafanaGet, GrafanaReadError } from "../apiClient";
import { useRouter } from "../router";
import type { Lang } from "../types";
import { AlertCircle, ExternalLink, LayoutDashboard, RefreshCw } from "@/components/icons";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Spinner } from "@/components/ui/spinner";
import { SelectControl } from "./primitives";
import {
  applicationDashboardSearchPath,
  applicationDashboardUrl,
  readApplicationDashboards,
  type ApplicationDashboard,
} from "./applicationDashboardModel";
import "./ObservabilityPanel.css";
import { ApplicationTaskBoard } from "./task-observability/ApplicationTaskBoard";

type DashboardList = {
  status: "loading" | "ready" | "error";
  dashboards: ApplicationDashboard[];
  failure?: "unauthorized" | "timeout" | "unavailable";
};

export function ApplicationDashboards({ app, lang, token }: { app: string; lang: Lang; token: string }) {
  return <ApplicationTaskBoard app={app} token={token} zh={lang === "zh"}
    metrics={<GrafanaDashboards app={app} lang={lang} token={token} />} />;
}

function GrafanaDashboards({ app, lang, token }: { app: string; lang: Lang; token: string }) {
  const zh = lang === "zh";
  const { path, search, navigate } = useRouter();
  const [list, setList] = useState<DashboardList>({ status: "loading", dashboards: [] });
  const [revision, setRevision] = useState(0);
  const [loadedSrc, setLoadedSrc] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    let disposed = false;
    const deadline = window.setTimeout(() => controller.abort(), 8000);
    setList({ status: "loading", dashboards: [] });
    void grafanaGet<unknown>(applicationDashboardSearchPath(app), token, controller.signal)
      .then((payload) => {
        const dashboards = readApplicationDashboards(payload, app);
        if (!disposed) setList({ status: "ready", dashboards });
      })
      .catch((error) => {
        if (disposed) return;
        const unauthorized = error instanceof GrafanaReadError && [401, 403].includes(error.status);
        setList({ status: "error", dashboards: [], failure: controller.signal.aborted ? "timeout"
          : unauthorized ? "unauthorized" : "unavailable" });
      })
      .finally(() => window.clearTimeout(deadline));
    return () => {
      disposed = true;
      window.clearTimeout(deadline);
      controller.abort();
    };
  }, [app, token, revision]);

  const requestedUid = new URLSearchParams(search).get("dashboard");
  const selected = list.dashboards.find((item) => item.uid === requestedUid) || list.dashboards[0];
  const src = selected ? applicationDashboardUrl(selected.uid, app, true, search) : "";
  const selectDashboard = (uid: string) => {
    const params = new URLSearchParams(search);
    params.set("dashboard", uid);
    navigate(`${path}?${params.toString()}`);
  };

  return (
    <Card className={selected ? "grafana-embed min-w-0" : "min-w-0"}>
      <CardContent className="flex min-h-0 flex-1 flex-col gap-4">
        <div className="flex flex-wrap items-center gap-3">
          {selected && (list.dashboards.length > 1 ? (
            <SelectControl
              value={selected.uid}
              options={list.dashboards.map((item) => ({ value: item.uid, label: item.title }))}
              onChange={selectDashboard}
              ariaLabel={zh ? "选择看板" : "Select dashboard"}
              className="w-full sm:max-w-sm"
            />
          ) : (
            <p className="min-w-0 truncate text-sm font-medium" title={selected.title}>{selected.title}</p>
          ))}
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" disabled={list.status === "loading"} onClick={() => setRevision((value) => value + 1)}>
              <RefreshCw data-icon="inline-start" />
              {zh ? "刷新列表" : "Refresh list"}
            </Button>
            {selected && (
              <a className={buttonVariants({ variant: "outline", size: "sm" })}
                href={applicationDashboardUrl(selected.uid, app, false, search)} target="_blank" rel="noopener noreferrer">
                <ExternalLink data-icon="inline-start" />
                {zh ? "新窗口打开" : "Open in new tab"}
              </a>
            )}
          </div>
        </div>
        {list.status === "loading" && (
          <div role="status" className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
            <Spinner aria-hidden="true" />
            {zh ? "正在加载看板列表…" : "Loading dashboards…"}
          </div>
        )}
        {list.status === "error" && (
          <Alert variant="warning">
            <AlertCircle />
            <AlertTitle>{zh ? "暂时无法加载看板" : "Dashboards are unavailable"}</AlertTitle>
            <AlertDescription>
              {list.failure === "unauthorized"
                ? (zh ? "登录会话或查询权限已失效，请重新登录后刷新列表。" : "The session or query permission has expired. Sign in again, then refresh the list.")
                : list.failure === "timeout"
                  ? (zh ? "看板查询超时，请稍后刷新列表。" : "The dashboard query timed out. Refresh the list shortly.")
                  : (zh ? "Grafana 查询暂时失败，请稍后刷新列表；若持续失败，请检查 Observe 服务。" : "The Grafana query failed temporarily. Refresh shortly; if it persists, check the Observe service.")}
            </AlertDescription>
          </Alert>
        )}
        {list.status === "ready" && !selected && (
          <Empty>
            <EmptyHeader>
              <EmptyMedia variant="icon"><LayoutDashboard /></EmptyMedia>
              <EmptyTitle>{zh ? "暂无自定义看板" : "No custom dashboards"}</EmptyTitle>
              <EmptyDescription>
                {zh ? "为此应用安装并关联看板后，刷新列表即可查看。" : "Install and associate a dashboard with this application, then refresh the list."}
              </EmptyDescription>
            </EmptyHeader>
            <EmptyContent>
              <a
                className={buttonVariants({ variant: "outline", size: "sm" })}
                href={`https://liutianjie.github.io/luma/docs/observe-applications${zh ? ".zh-CN" : ""}.html#link-dashboards-to-an-application`}
                target="_blank"
                rel="noopener noreferrer"
              >
                {zh ? "查看配置说明" : "Configuration guide"}
                <ExternalLink data-icon="inline-end" />
              </a>
            </EmptyContent>
          </Empty>
        )}
        {selected && (
          <>
            <p className="text-xs leading-relaxed text-muted-foreground">
              {zh
                ? "在看板内调整时间和筛选。打开时会带入排查范围；服务、环境和实例筛选仅对使用对应变量的图表生效。"
                : "Adjust time and filters inside the dashboard. The investigation range is applied when opening it; service, environment and instance filters affect only charts using those variables."}
            </p>
            {loadedSrc !== src && (
              <div role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
                <Spinner aria-hidden="true" />
                {zh ? "正在加载看板…" : "Loading dashboard…"}
              </div>
            )}
            <iframe
              key={src}
              src={src}
              title={`${app} · ${selected.title}`}
              className="min-h-0 w-full flex-1 rounded-md border border-border bg-background"
              onLoad={() => setLoadedSrc(src)}
              allowFullScreen
            />
          </>
        )}
      </CardContent>
    </Card>
  );
}
