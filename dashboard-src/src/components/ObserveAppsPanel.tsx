import { useEffect, useId, useRef, useState } from "react";
import { Maximize2, Minimize2 } from "lucide-react";
import type { Lang } from "../types";
import { SelectControl } from "./primitives";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
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
function tracesExploreSrc(app: string): string {
  const stack = app.replace(/[^a-zA-Z0-9._-]/g, "");
  const query = stack
    ? `{ resource.luma.stack = "${stack}" }`
    : '{ resource.service.name = "traefik" || resource.luma.stack =~ ".+" }';
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
          range: { from: "now-1h", to: "now" },
        },
      }),
    )
  );
}

function logsExploreSrc(app: string): string {
  const stack = app.replace(/[^a-zA-Z0-9._-]/g, "");
  const expr = stack ? `app:="${stack}"` : 'app:~".+"';
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
          range: { from: "now-1h", to: "now" },
        },
      }),
    )
  );
}

function dashboardSrc(uid: string, app: string): string {
  const params = new URLSearchParams({
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
  const container = useRef<HTMLDivElement>(null);
  const fieldId = useId();
  const [expanded, setExpanded] = useState(false);
  const [loadedSrc, setLoadedSrc] = useState("");
  const [fullscreenError, setFullscreenError] = useState("");
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
  const [view, setView] = useState<(typeof VIEWS)[number]["id"]>(initialView);
  const [app, setApp] = useState(initialApp.replace(/[^a-zA-Z0-9._-]/g, ""));
  useEffect(() => {
    setView(initialView);
  }, [initialView]);
  useEffect(() => {
    setApp(initialApp.replace(/[^a-zA-Z0-9._-]/g, ""));
  }, [initialApp]);
  const names = [...new Set([...applicationNames, app].filter(Boolean))].sort();
  const current = VIEWS.find((item) => item.id === view) || VIEWS[0];
  const src =
    view === "traces"
      ? tracesExploreSrc(app)
      : view === "logs"
        ? logsExploreSrc(app)
        : view === "grafana"
          ? "/grafana/?orgId=1"
          : dashboardSrc(current.uid, app);
  const showAppFilter = view !== "grafana";
  return (
    <Card ref={container} className="grafana-embed min-w-0">
      <CardHeader>
        <CardTitle>
          {lockedView && app ? `${app} · ` : ""}
          {zh ? current.zh : current.en}
        </CardTitle>
        <CardDescription>
          {loadedSrc !== src ? (
            <span role="status" className="flex items-center gap-2">
              <Spinner aria-hidden="true" />
              {zh ? "正在加载观测面板…" : "Loading observability dashboard…"}
            </span>
          ) : zh ? (
            "Grafana 观测面板"
          ) : (
            "Grafana observability dashboard"
          )}
        </CardDescription>
        {document.fullscreenEnabled && (
          <CardAction>
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-pressed={expanded}
              onClick={() => void toggleFullscreen()}
            >
              {expanded ? (
                <Minimize2 data-icon="inline-start" />
              ) : (
                <Maximize2 data-icon="inline-start" />
              )}
              {expanded
                ? zh
                  ? "退出全屏"
                  : "Exit fullscreen"
                : zh
                  ? "全屏"
                  : "Fullscreen"}
            </Button>
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col gap-4">
        {!lockedView && (
          <FieldGroup className="flex flex-col flex-wrap lg:flex-row lg:items-end">
            <Field className="min-w-0 lg:flex-1">
              <FieldLabel id={`${fieldId}-views`}>
                {zh ? "视图" : "View"}
              </FieldLabel>
              <ToggleGroup
                variant="outline"
                value={[view]}
                onValueChange={(values) => {
                  if (values[0])
                    setView(values[0] as (typeof VIEWS)[number]["id"]);
                }}
                aria-labelledby={`${fieldId}-views`}
                className="flex-wrap"
                spacing={1}
              >
                {VIEWS.map((item) => (
                  <ToggleGroupItem key={item.id} value={item.id}>
                    {zh ? item.zh : item.en}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
            </Field>
            {showAppFilter && (
              <Field className="lg:w-56">
                <FieldLabel htmlFor={`${fieldId}-app`}>
                  {zh ? "应用" : "Application"}
                </FieldLabel>
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
              </Field>
            )}
          </FieldGroup>
        )}
        {fullscreenError && (
          <Alert>
            <AlertTitle>
              {zh ? "全屏不可用" : "Fullscreen unavailable"}
            </AlertTitle>
            <AlertDescription>{fullscreenError}</AlertDescription>
          </Alert>
        )}
        <iframe
          key={src}
          title={zh ? current.zh : current.en}
          src={src}
          allow="fullscreen"
          className="min-h-0 w-full flex-1 border-0"
          aria-busy={loadedSrc !== src}
          onLoad={() => setLoadedSrc(src)}
        />
      </CardContent>
    </Card>
  );
}
