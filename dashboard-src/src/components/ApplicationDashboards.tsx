import { useEffect, useState } from "react";
import { apiGet } from "../apiClient";
import { useRouter } from "../router";
import type { Lang } from "../types";
import { AlertCircle, ExternalLink, LayoutDashboard, RefreshCw } from "@/components/icons";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
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

type DashboardList = {
  status: "loading" | "ready" | "error";
  dashboards: ApplicationDashboard[];
};

export function ApplicationDashboards({ app, lang, token }: { app: string; lang: Lang; token: string }) {
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
    void apiGet<unknown>(applicationDashboardSearchPath(app), token, controller.signal)
      .then((payload) => {
        const dashboards = readApplicationDashboards(payload, app);
        if (!disposed) setList({ status: "ready", dashboards });
      })
      .catch(() => {
        if (!disposed) setList({ status: "error", dashboards: [] });
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
  const src = selected ? applicationDashboardUrl(selected.uid, app) : "";
  const selectDashboard = (uid: string) => {
    const params = new URLSearchParams(search);
    params.set("dashboard", uid);
    navigate(`${path}?${params.toString()}`);
  };

  return (
    <Card className={selected ? "grafana-embed min-w-0" : "min-w-0"}>
      <CardHeader>
        <CardTitle>{zh ? "自定义看板" : "Custom dashboards"}</CardTitle>
        <CardDescription>{zh ? "查看此应用关联的 Grafana 看板。" : "Grafana dashboards associated with this application."}</CardDescription>
        <CardAction>
          <Button variant="outline" size="sm" disabled={list.status === "loading"} onClick={() => setRevision((value) => value + 1)}>
            <RefreshCw data-icon="inline-start" />
            {zh ? "刷新列表" : "Refresh list"}
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col gap-4">
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
              {zh ? "请确认 Observe 已启用且 Grafana 可以访问，然后刷新列表重试。" : "Check that Observe is enabled and Grafana is reachable, then refresh the list to retry."}
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
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
              {list.dashboards.length > 1 ? (
                <SelectControl
                  value={selected.uid}
                  options={list.dashboards.map((item) => ({ value: item.uid, label: item.title }))}
                  onChange={selectDashboard}
                  ariaLabel={zh ? "选择看板" : "Select dashboard"}
                  className="sm:max-w-sm"
                />
              ) : (
                <p className="min-w-0 truncate text-sm font-medium" title={selected.title}>{selected.title}</p>
              )}
              <a
                className={buttonVariants({ variant: "outline", size: "sm", className: "sm:ml-auto" })}
                href={applicationDashboardUrl(selected.uid, app, false)}
                target="_blank"
                rel="noopener noreferrer"
              >
                <ExternalLink data-icon="inline-start" />
                {zh ? "新窗口打开" : "Open in new tab"}
              </a>
            </div>
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
