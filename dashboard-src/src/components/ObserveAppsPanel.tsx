import { useRouter } from "../router";
import { grafanaRange, logsExpression, observationQuery } from "../observeContext";
import { useEffect, useId, useRef, useState } from "react";
import { ExternalLink, Maximize2, Minimize2 } from "@/components/icons";
import { cn } from "@/lib/utils";
import { useObserveRefresh, useRefreshEvent } from "./observeRefresh";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Lang } from "../types";
import { SelectControl } from "./primitives";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Spinner } from "@/components/ui/spinner";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import "./ObservabilityPanel.css";

const VIEWS = [
  { id: "http", uid: "luma-http-apps", zh: "HTTP 应用", en: "HTTP apps" },
  { id: "nomad", uid: "luma-nomad-jobs", zh: "Nomad", en: "Nomad" },
  { id: "nodes", uid: "luma-nodes", zh: "节点", en: "Nodes" },
  { id: "traces", uid: "", zh: "链路", en: "Traces" },
  { id: "logs", uid: "", zh: "日志", en: "Logs" },
  { id: "grafana", uid: "", zh: "Grafana", en: "Grafana" },
] as const;

// Grafana OSS 11.5 dashboard panels query Tempo through /api/ds/query, which
// rejects TraceQL search ("backend TraceQL search queries are not supported").
// Explore uses the datasource proxy instead. kiosk=tv on Explore sends Trace ID
// clicks to Grafana home, so this tab is not kiosk.
function tracesExploreSrc(app: string, search: string): string {
  const stack = app.replace(/[^a-zA-Z0-9._-]/g, "");
  let query = stack
    ? `{ resource.luma.stack = "${stack}" }`
    : '{ (resource.service.name = "traefik" || resource.luma.stack =~ ".+") }';
  const scope = observationQuery(search);
  const filters: string[] = [];
  for (const [key,field] of [["environment","deployment.environment.name"],["service","service.name"],["instance","service.instance.id"]]) {
    if (scope[key]) filters.push(`resource.${field} = ${JSON.stringify(scope[key])}`);
  }
  const id = new URLSearchParams(search).get("observeTrace");
  if (scope.keyword) filters.push(`name =~ ${JSON.stringify(".*" + String(scope.keyword).replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ".*")}`);
  if (filters.length) query = query.replace(/}$/, ` && ${filters.join(" && ")} }`);
  if (id && /^[a-f0-9]{32}$/i.test(id)) query = id.toLowerCase();
  return (
    "/grafana/explore?orgId=1&schemaVersion=1&panes=" +
    encodeURIComponent(
      JSON.stringify({
        traces: {
          datasource: "tempo",
          queries: [
            {
              refId: "A",
              datasource: { type: "tempo", uid: "tempo" },
              queryType: "traceql",
              query,
              limit: 20,
              tableType: "traces",
            },
          ],
          range: grafanaRange(search),
        },
      }),
    )
  );
}

function logsExploreSrc(app: string, search: string): string {
  const stack = app.replace(/[^a-zA-Z0-9._-]/g, "");
  const expr = logsExpression(stack, search);
  return (
    "/grafana/explore?orgId=1&schemaVersion=1&panes=" +
    encodeURIComponent(
      JSON.stringify({
        logs: {
          datasource: "victorialogs",
          queries: [
            {
              refId: "A",
              datasource: {
                type: "victoriametrics-logs-datasource",
                uid: "victorialogs",
              },
              queryType: "instant",
              expr,
              maxLines: 1000,
            },
          ],
          range: grafanaRange(search),
        },
      }),
    )
  );
}

function dashboardSrc(uid: string, app: string, search: string): string {
  const params = new URLSearchParams({
    ...grafanaRange(search),
    orgId: "1",
    kiosk: "tv",
    autofitpanels: "true",
  });
  if (app) params.set("var-app", app);
  return `/grafana/d/${uid}?${params.toString()}`;
}

export function ObserveAppsPanel({
  lang,
  applicationNames = [],
  initialView = "http",
  initialApp = "",
  lockedView = false,
}: {
  lang: Lang;
  token: string;
  applicationNames?: string[];
  initialView?: (typeof VIEWS)[number]["id"];
  initialApp?: string;
  lockedView?: boolean;
}) {
  const zh = lang === "zh";
  const { path, search, navigate } = useRouter();
  const container = useRef<HTMLDivElement>(null);
  const fieldId = useId();
  const [expanded, setExpanded] = useState(false);
  const [loadedSrc, setLoadedSrc] = useState("");
  const [fullscreenError, setFullscreenError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const refreshHost = useObserveRefresh();
  // The page refresh reloads the embedded view in place.
  useRefreshEvent(() => {
    setLoadedSrc("");
    setReloadKey((n) => n + 1);
  });
  useEffect(() => {
    const onFullscreenChange = () =>
      setExpanded(document.fullscreenElement === container.current);
    document.addEventListener("fullscreenchange", onFullscreenChange);
    return () =>
      document.removeEventListener("fullscreenchange", onFullscreenChange);
  }, []);
  const toggleFullscreen = async () => {
    setFullscreenError("");
    try {
      if (document.fullscreenElement === container.current)
        await document.exitFullscreen();
      else await container.current?.requestFullscreen();
    } catch {
      setFullscreenError(
        zh
          ? "浏览器未能进入全屏，请在当前页面继续查看。"
          : "The browser could not enter fullscreen. Continue in the current view.",
      );
    }
  };
  const params = new URLSearchParams(search);
  const requested = params.get("view");
  const view = lockedView ? initialView : VIEWS.find(v=>v.id===requested)?.id || initialView;
  const app = lockedView ? initialApp : params.get("app") ?? initialApp;
  const update = (key: string, value: string) => {const next = new URLSearchParams(search); next.set(key,value); navigate(`${path}?${next}`);};
  const setView = (value: string) => update("view",value);
  const setApp = (value: string) => update("app",value);
  const names = [...new Set([...applicationNames, app].filter(Boolean))].sort();
  const current = VIEWS.find((item) => item.id === view) || VIEWS[0];
  const src =
    view === "traces"
      ? tracesExploreSrc(app, search)
      : view === "logs"
        ? logsExploreSrc(app, search)
        : view === "grafana"
          ? "/grafana/?orgId=1"
          : dashboardSrc(current.uid, app, search);
  const showAppFilter = view !== "grafana";
  const loading = loadedSrc !== src;
  const openHref = src.replace(/([?&])kiosk=tv&?/, "$1").replace(/[?&]$/, "");
  return (
    <Card ref={container} className="grafana-embed min-w-0 gap-0 py-0">
      <div className="flex flex-wrap items-center gap-2 border-b border-hairline px-4 py-2.5" role="toolbar" aria-label={zh ? "观测视图" : "Observability view"}>
        {lockedView ? (
          <span className="text-[0.8125rem] font-medium">
            {app ? `${app} · ` : ""}
            {zh ? current.zh : current.en}
          </span>
        ) : (
          <ToggleGroup
            variant="segmented"
            value={[view]}
            onValueChange={(values) => {
              if (values[0])
                setView(values[0] as (typeof VIEWS)[number]["id"]);
            }}
            aria-label={zh ? "视图" : "View"}
          >
            {VIEWS.map((item) => (
              <ToggleGroupItem key={item.id} value={item.id}>
                {zh ? item.zh : item.en}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        )}
        <div className="ml-auto flex min-w-0 items-center gap-2">
          {loading && (
            <span role="status" className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Spinner aria-hidden="true" />
              {zh ? "正在加载…" : "Loading…"}
            </span>
          )}
          {!lockedView && showAppFilter && (
            <div className="w-48 min-w-0">
              <label htmlFor={`${fieldId}-app`} className="sr-only">
                {zh ? "应用" : "Application"}
              </label>
              <SelectControl
                id={`${fieldId}-app`}
                className="min-w-0"
                value={app}
                onChange={setApp}
                options={[
                  { value: "", label: zh ? "全部应用" : "All apps" },
                  ...names.map((name) => ({ value: name, label: name })),
                ]}
              />
            </div>
          )}
          <Tooltip>
            <TooltipTrigger render={<Button variant="ghost" size="icon-sm" nativeButton={false} render={<a href={openHref} target="_blank" rel="noopener noreferrer" />} aria-label={zh ? "在 Grafana 中打开（新窗口）" : "Open in Grafana (new window)"} />}>
              <ExternalLink />
            </TooltipTrigger>
            <TooltipContent>{zh ? "在 Grafana 中打开" : "Open in Grafana"}</TooltipContent>
          </Tooltip>
          {document.fullscreenEnabled && (
            <Tooltip>
              <TooltipTrigger render={<Button type="button" variant="ghost" size="icon-sm" aria-pressed={expanded} aria-label={expanded ? (zh ? "退出全屏" : "Exit fullscreen") : (zh ? "全屏" : "Fullscreen")} onClick={() => void toggleFullscreen()} />}>
                {expanded ? <Minimize2 /> : <Maximize2 />}
              </TooltipTrigger>
              <TooltipContent>{expanded ? (zh ? "退出全屏" : "Exit fullscreen") : (zh ? "全屏" : "Fullscreen")}</TooltipContent>
            </Tooltip>
          )}
        </div>
      </div>
      {fullscreenError && (
        <Alert className="m-4 w-auto">
          <AlertTitle>
            {zh ? "全屏不可用" : "Fullscreen unavailable"}
          </AlertTitle>
          <AlertDescription>{fullscreenError}</AlertDescription>
        </Alert>
      )}
      <div className="relative min-h-0 flex-1 bg-background">
        {loading && <Skeleton aria-hidden="true" className="absolute inset-4 rounded-md" />}
        <iframe
          key={`${src}:${reloadKey}`}
          title={zh ? current.zh : current.en}
          src={src}
          allow="fullscreen"
          className={cn("relative size-full border-0 transition-opacity duration-200", loading ? "opacity-0" : "opacity-100")}
          aria-busy={loading}
          onLoad={() => {
            setLoadedSrc(src);
            refreshHost.markUpdated();
          }}
        />
      </div>
    </Card>
  );
}
