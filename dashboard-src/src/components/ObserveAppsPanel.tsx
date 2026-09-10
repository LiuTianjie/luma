import { useState } from "react";
import type { Lang } from "../types";
import "./ObservabilityPanel.css";

const VIEWS = [
  { id: "http", src: "/grafana/d/luma-http-apps?orgId=1&kiosk=tv", zh: "HTTP 应用", en: "HTTP apps" },
  { id: "nomad", src: "/grafana/d/luma-nomad-jobs?orgId=1&kiosk=tv", zh: "Nomad", en: "Nomad" },
  { id: "traces", src: "/grafana/d/luma-traces?orgId=1&kiosk=tv", zh: "链路", en: "Traces" },
  { id: "grafana", src: "/grafana/?orgId=1", zh: "Grafana", en: "Grafana" },
] as const;

export function ObserveAppsPanel({ lang }: { lang: Lang; token: string }) {
  const zh = lang === "zh";
  const [view, setView] = useState<(typeof VIEWS)[number]["id"]>("http");
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
        <small>{zh ? "Grafana 嵌在控制面域名 /grafana，observe 未部署时这里会空白。" : "Grafana is embedded at /grafana on the Control domain. Empty if observe is not deployed."}</small>
      </div>
      <iframe title={zh ? current.zh : current.en} src={current.src} allow="fullscreen" />
    </div>
  );
}
