import { useEffect, useState } from "react";
import { fetchObserveApps, type ObserveAppsPayload } from "../observeApi";
import type { Lang } from "../types";
import { TrendChart } from "./charts";
import "./ObservabilityPanel.css";

const WINDOWS = [900, 3600, 21600];

function formatRate(value: number) {
  if (value >= 10) return value.toFixed(1);
  if (value >= 1) return value.toFixed(2);
  return value.toFixed(3);
}

function formatRatio(value: number) {
  return `${(value * 100).toFixed(2)}%`;
}

export function ObserveAppsPanel({ lang, token }: { lang: Lang; token: string }) {
  const zh = lang === "zh";
  const [windowSec, setWindowSec] = useState(3600);
  const [payload, setPayload] = useState<ObserveAppsPayload | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    async function load() {
      try {
        const next = await fetchObserveApps(token, windowSec, controller.signal);
        if (!cancelled) {
          setPayload(next);
          setError("");
        }
      } catch (err) {
        if (!cancelled && !controller.signal.aborted) setError(err instanceof Error ? err.message : String(err));
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 15000);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [token, windowSec]);
  return (
    <div className="metrics-workspace">
      <div className="history-toolbar">
        <label>
          {zh ? "时间范围" : "Time range"}
          <select value={windowSec} onChange={(event) => setWindowSec(Number(event.target.value))}>
            {WINDOWS.map((value) => (
              <option value={value} key={value}>{value < 3600 ? `${value / 60} min` : `${value / 3600} h`}</option>
            ))}
          </select>
        </label>
        <small>{zh ? "公网入口请求和 Nomad 任务健康，不是容器内部 /metrics。" : "Public HTTP entrypoints and Nomad job health, not container /metrics."}</small>
      </div>
      {error && !payload ? <small className="history-status history-status-warning">{error}</small> : null}
      {payload && !payload.available ? <small className="history-status history-status-warning">{payload.message || (zh ? "luma-observe 暂不可用" : "luma-observe is unavailable")}</small> : null}
      {payload?.available ? (
        <>
          <section className="panel">
            <div className="panel-heading"><h2>{zh ? "公网入口" : "Public HTTP"}</h2></div>
            {!payload.http.length ? <small className="history-status">{zh ? "还没有 Traefik 请求样本" : "No Traefik request samples yet"}</small> : null}
            {payload.http.map((item) => (
              <div className="service-history-detail" key={item.id}>
                <strong>{item.id}</strong>
                <div className="metrics-summary">
                  <span><small>QPS</small><strong>{formatRate(item.requestRate)}</strong></span>
                  <span><small>5xx</small><strong>{formatRate(item.errorRate)}</strong></span>
                  <span><small>5xx %</small><strong>{formatRatio(item.errorRatio)}</strong></span>
                </div>
                <div className="service-history-charts">
                  <div><h3>{zh ? "请求" : "Requests"}</h3><TrendChart points={item.requests} format={formatRate} height={120} emptyLabel={zh ? "等待采样" : "Waiting"} /></div>
                  <div><h3>5xx</h3><TrendChart points={item.errors} format={formatRate} height={120} emptyLabel={zh ? "等待采样" : "Waiting"} /></div>
                </div>
              </div>
            ))}
          </section>
          <section className="panel">
            <div className="panel-heading"><h2>Nomad</h2></div>
            {!payload.jobs.length ? <small className="history-status">{zh ? "还没有 job 样本" : "No job samples yet"}</small> : null}
            {payload.jobs.map((item) => (
              <div className="service-history-detail" key={item.id}>
                <strong>{item.id}</strong>
                <div className="metrics-summary">
                  <span><small>{zh ? "运行中" : "Running"}</small><strong>{item.running}</strong></span>
                  <span><small>Failed</small><strong>{item.failed}</strong></span>
                </div>
                <div className="service-history-charts">
                  <div><h3>{zh ? "运行中" : "Running"}</h3><TrendChart points={item.runningPoints} format={(value) => String(Math.round(value))} height={120} emptyLabel={zh ? "等待采样" : "Waiting"} /></div>
                  <div><h3>Failed</h3><TrendChart points={item.failedPoints} format={(value) => String(Math.round(value))} height={120} emptyLabel={zh ? "等待采样" : "Waiting"} /></div>
                </div>
              </div>
            ))}
          </section>
        </>
      ) : null}
    </div>
  );
}
