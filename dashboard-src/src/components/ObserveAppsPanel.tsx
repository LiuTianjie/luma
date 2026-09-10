import { useState } from "react";
import type { Lang } from "../types";
import "./ObservabilityPanel.css";

const VIEWS = [
  { id: "http", zh: "HTTP 应用", en: "HTTP apps" },
  { id: "nomad", zh: "Nomad", en: "Nomad" },
  { id: "traces", zh: "链路", en: "Traces" },
  { id: "grafana", zh: "Grafana", en: "Grafana" },
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

function dashboardSrc(uid: string, app: string): string {
  const params = new URLSearchParams({ orgId: "1", kiosk: "tv" });
  if (app) params.set("var-app", app);
  return `/grafana/d/${uid}?${params.toString()}`;
}

export function ObserveAppsPanel({
  lang,
  applicationNames = [],
}: {
  lang: Lang;
  token: string;
  applicationNames?: string[];
}) {
  const zh = lang === "zh";
  const [view, setView] = useState<(typeof VIEWS)[number]["id"]>("http");
  const [app, setApp] = useState("");
  const names = [...new Set(applicationNames.filter(Boolean))].sort();
  const src =
    view === "traces"
      ? tracesExploreSrc(app)
      : view === "grafana"
        ? "/grafana/?orgId=1"
        : dashboardSrc(view === "http" ? "luma-http-apps" : "luma-nomad-jobs", app);
  const current = VIEWS.find((item) => item.id === view) || VIEWS[0];
  return (
    <div className="metrics-workspace grafana-embed">
      <div className="history-toolbar grafana-toolbar">
        <div className="grafana-view-switch" role="tablist" aria-label={zh ? "Grafana 面板" : "Grafana dashboards"}>
          {VIEWS.map((item) => (
            <button
              key={item.id}
              type="button"
              className="ghost"
              role="tab"
              aria-selected={view === item.id}
              onClick={() => setView(item.id)}
            >
              {zh ? item.zh : item.en}
            </button>
          ))}
        </div>
        {view !== "grafana" ? (
          <select
            className="grafana-app-filter"
            value={app}
            aria-label={zh ? "按应用筛选" : "Filter by app"}
            onChange={(event) => setApp(event.target.value)}
          >
            <option value="">{zh ? "全部应用" : "All apps"}</option>
            {names.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </select>
        ) : null}
        <small>
          {view === "traces"
            ? zh
              ? "点 Trace ID 打开 Tempo。按应用筛选匹配 luma.stack；未插桩的应用请选「全部」看 Traefik 入口 span。"
              : "Click a Trace ID to open Tempo. App filter matches luma.stack; Traefik ingress spans are under All."
            : zh
              ? "Grafana 嵌在控制面域名 /grafana，observe 未部署时这里会空白。"
              : "Grafana is embedded at /grafana on the Control domain. Empty if observe is not deployed."}
        </small>
      </div>
      <iframe key={src} title={zh ? current.zh : current.en} src={src} allow="fullscreen" />
    </div>
  );
}
