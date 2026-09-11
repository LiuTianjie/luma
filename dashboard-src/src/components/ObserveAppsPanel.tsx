import { useState } from "react";
import type { Lang } from "../types";
import { SelectControl } from "./primitives";
import { Button } from "@/components/ui/button";
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
              datasource: { type: "victoriametrics-logs-datasource", uid: "victorialogs" },
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
  const params = new URLSearchParams({ orgId: "1", kiosk: "tv", autofitpanels: "true" });
  if (app) params.set("var-app", app);
  return `/grafana/d/${uid}?${params.toString()}`;
}

export function ObserveAppsPanel({
  lang,
  applicationNames = [],
  initialView = "http",
  initialApp = "",
}: {
  lang: Lang;
  token: string;
  applicationNames?: string[];
  initialView?: (typeof VIEWS)[number]["id"];
  initialApp?: string;
}) {
  const zh = lang === "zh";
  const [view, setView] = useState<(typeof VIEWS)[number]["id"]>(initialView);
  const [app, setApp] = useState(initialApp.replace(/[^a-zA-Z0-9._-]/g, ""));
  const names = [...new Set(applicationNames.filter(Boolean))].sort();
  const current = VIEWS.find((item) => item.id === view) || VIEWS[0];
  const src =
    view === "traces"
      ? tracesExploreSrc(app)
      : view === "logs"
        ? logsExploreSrc(app)
        : view === "grafana"
          ? "/grafana/?orgId=1"
          : dashboardSrc(current.uid, app);
  const showAppFilter = view === "http" || view === "nomad" || view === "nodes" || view === "traces" || view === "logs";
  return (
    <div className="metrics-workspace grafana-embed">
      <div className="history-toolbar grafana-toolbar">
        <div className="flex flex-wrap gap-1" role="tablist" aria-label={zh ? "Grafana 面板" : "Grafana dashboards"}>
          {VIEWS.map((item) => (
            <Button
              key={item.id}
              type="button"
              size="sm"
              variant={view === item.id ? "secondary" : "ghost"}
              role="tab"
              aria-selected={view === item.id}
              onClick={() => setView(item.id)}
            >
              {zh ? item.zh : item.en}
            </Button>
          ))}
        </div>
        {showAppFilter ? (
          <SelectControl
            className="w-56 min-w-0"
            ariaLabel={zh ? "按应用筛选" : "Filter by app"}
            value={app}
            onChange={setApp}
            options={[
              { value: "", label: zh ? "全部应用" : "All apps" },
              ...names.map((name) => ({ value: name, label: name })),
            ]}
          />
        ) : null}
      </div>
      <iframe key={src} title={zh ? current.zh : current.en} src={src} allow="fullscreen" />
    </div>
  );
}
