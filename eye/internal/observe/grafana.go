package observe

import (
	"encoding/json"
	"fmt"
)

// RenderGrafanaDashboard generates a standard Grafana 11.5 dashboard JSON tagged with
// `luma-app:<project>` so Luma's Application Details -> Dashboards tab auto-discovers it.
// datasourceMode: "victoria" (zero-plugin Luma Observe default) or "clickhouse" (SQL panels).
func RenderGrafanaDashboard(projectSlug, datasourceMode string) ([]byte, error) {
	if projectSlug == "" {
		return nil, fmt.Errorf("project slug is required")
	}
	if datasourceMode == "" {
		datasourceMode = "victoria"
	}

	uid := fmt.Sprintf("luma-eye-%s", projectSlug)
	if len(uid) > 40 {
		uid = uid[:40]
	}

	var panels []map[string]any
	switch datasourceMode {
	case "clickhouse":
		ds := map[string]any{"type": "grafana-clickhouse-datasource", "uid": "clickhouse"}
		panels = []map[string]any{
			{
				"id": 1, "type": "stat", "title": "Unique Visitors (Selected Window)",
				"gridPos": map[string]int{"x": 0, "y": 0, "w": 6, "h": 4},
				"datasource": ds,
				"targets": []map[string]any{{
					"refId": "A", "format": 1,
					"rawSql": fmt.Sprintf("SELECT uniqCombinedMerge(12)(uv_state) AS uv FROM luma_eye.events_hourly WHERE project = '%s' AND $__timeFilter(hour)", projectSlug),
				}},
			},
			{
				"id": 2, "type": "stat", "title": "Pageviews (Selected Window)",
				"gridPos": map[string]int{"x": 6, "y": 0, "w": 6, "h": 4},
				"datasource": ds,
				"targets": []map[string]any{{
					"refId": "A", "format": 1,
					"rawSql": fmt.Sprintf("SELECT sumIf(event_count, event_type = 'pageview') AS pv FROM luma_eye.events_hourly WHERE project = '%s' AND $__timeFilter(hour)", projectSlug),
				}},
			},
			{
				"id": 3, "type": "stat", "title": "Frontend Exceptions",
				"gridPos": map[string]int{"x": 12, "y": 0, "w": 6, "h": 4},
				"datasource": ds,
				"targets": []map[string]any{{
					"refId": "A", "format": 1,
					"rawSql": fmt.Sprintf("SELECT sum(total_count) AS errors FROM luma_eye.issue_rollups WHERE project = '%s' AND $__timeFilter(last_seen)", projectSlug),
				}},
			},
			{
				"id": 4, "type": "stat", "title": "LCP P75 (ms)",
				"gridPos": map[string]int{"x": 18, "y": 0, "w": 6, "h": 4},
				"datasource": ds,
				"targets": []map[string]any{{
					"refId": "A", "format": 1,
					"rawSql": fmt.Sprintf("SELECT quantileExact(0.75)(value) AS lcp_p75 FROM luma_eye.vitals WHERE project = '%s' AND metric_name = 'LCP' AND $__timeFilter(ts)", projectSlug),
				}},
			},
			{
				"id": 5, "type": "timeseries", "title": "Hourly Pageviews & Custom Events",
				"gridPos": map[string]int{"x": 0, "y": 4, "w": 12, "h": 8},
				"datasource": ds,
				"targets": []map[string]any{{
					"refId": "A", "format": 0,
					"rawSql": fmt.Sprintf("SELECT hour AS time, sumIf(event_count, event_type = 'pageview') AS pageviews, sumIf(event_count, event_type = 'custom') AS custom_events FROM luma_eye.events_hourly WHERE project = '%s' AND $__timeFilter(hour) GROUP BY time ORDER BY time", projectSlug),
				}},
			},
			{
				"id": 6, "type": "table", "title": "Top Client Issues",
				"gridPos": map[string]int{"x": 12, "y": 4, "w": 12, "h": 8},
				"datasource": ds,
				"targets": []map[string]any{{
					"refId": "A", "format": 1,
					"rawSql": fmt.Sprintf("SELECT fingerprint, anyLast(error_type) AS type, anyLast(error_message) AS message, anyLast(culprit) AS culprit, sum(total_count) AS occurrences FROM luma_eye.issue_rollups WHERE project = '%s' GROUP BY fingerprint ORDER BY max(last_seen) DESC LIMIT 20", projectSlug),
				}},
			},
		}
	default:
		// Zero-plugin VictoriaMetrics mode works out-of-the-box with stock Luma Observe!
		ds := map[string]any{"type": "prometheus", "uid": "victoria"}
		panels = []map[string]any{
			{
				"id": 1, "type": "stat", "title": "Pageviews (Rate / min)",
				"gridPos": map[string]int{"x": 0, "y": 0, "w": 6, "h": 4},
				"datasource": ds,
				"targets": []map[string]any{{
					"refId": "A",
					"expr":  fmt.Sprintf(`sum(rate(luma_eye_pageviews_total{project=%q}[5m])) * 60`, projectSlug),
				}},
			},
			{
				"id": 2, "type": "stat", "title": "Events (24h Increase)",
				"gridPos": map[string]int{"x": 6, "y": 0, "w": 6, "h": 4},
				"datasource": ds,
				"targets": []map[string]any{{
					"refId": "A",
					"expr":  fmt.Sprintf(`sum(increase(luma_eye_events_total{project=%q}[24h]))`, projectSlug),
				}},
			},
			{
				"id": 3, "type": "stat", "title": "Client Exceptions (24h Increase)",
				"gridPos": map[string]int{"x": 12, "y": 0, "w": 6, "h": 4},
				"datasource": ds,
				"targets": []map[string]any{{
					"refId": "A",
					"expr":  fmt.Sprintf(`sum(increase(luma_eye_exceptions_total{project=%q}[24h]))`, projectSlug),
				}},
			},
			{
				"id": 4, "type": "stat", "title": "Client Logs (24h Increase)",
				"gridPos": map[string]int{"x": 18, "y": 0, "w": 6, "h": 4},
				"datasource": ds,
				"targets": []map[string]any{{
					"refId": "A",
					"expr":  fmt.Sprintf(`sum(increase(luma_eye_logs_total{project=%q}[24h]))`, projectSlug),
				}},
			},
			{
				"id": 5, "type": "timeseries", "title": "Client Telemetry Rate by Signal",
				"gridPos": map[string]int{"x": 0, "y": 4, "w": 24, "h": 8},
				"datasource": ds,
				"targets": []map[string]any{
					{"refId": "A", "legendFormat": "Events/s", "expr": fmt.Sprintf(`sum(rate(luma_eye_events_total{project=%q}[1m]))`, projectSlug)},
					{"refId": "B", "legendFormat": "Pageviews/s", "expr": fmt.Sprintf(`sum(rate(luma_eye_pageviews_total{project=%q}[1m]))`, projectSlug)},
					{"refId": "C", "legendFormat": "Exceptions/s", "expr": fmt.Sprintf(`sum(rate(luma_eye_exceptions_total{project=%q}[1m]))`, projectSlug)},
					{"refId": "D", "legendFormat": "Vitals/s", "expr": fmt.Sprintf(`sum(rate(luma_eye_vitals_total{project=%q}[1m]))`, projectSlug)},
				},
			},
		}
	}

	dashboard := map[string]any{
		"uid":           uid,
		"title":         fmt.Sprintf("Luma Eye · %s", projectSlug),
		"tags":          []string{fmt.Sprintf("luma-app:%s", projectSlug), "luma-eye"},
		"schemaVersion": 39,
		"version":       1,
		"refresh":       "30s",
		"time": map[string]string{
			"from": "now-24h",
			"to":   "now",
		},
		"panels": panels,
	}
	return json.MarshalIndent(dashboard, "", "  ")
}
