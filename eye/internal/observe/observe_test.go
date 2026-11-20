package observe

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestGrafanaRenderAndOTLPPayload(t *testing.T) {
	raw, err := RenderGrafanaDashboard("demo-app", "victoria")
	if err != nil {
		t.Fatal(err)
	}
	var dash struct {
		UID   string   `json:"uid"`
		Tags  []string `json:"tags"`
		Title string   `json:"title"`
	}
	if err := json.Unmarshal(raw, &dash); err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(dash.Tags, "luma-app:demo-app") {
		t.Fatalf("expected luma-app:demo-app tag, got %+v", dash.Tags)
	}

	m := NewMetrics()
	m.RecordEvents("demo-app", 10, 4)
	prom := m.RenderPrometheus()
	if !strings.Contains(prom, `luma_eye_events_total{project="demo-app"} 10`) {
		t.Fatalf("unexpected prometheus output: %s", prom)
	}

	pusher := NewOTLPPusher("http://127.0.0.1:4319", map[string]string{"Authorization": "Bearer tok"}, "luma-eye", map[string]string{"luma.stack": "luma-eye"}, m)
	otlpBytes, err := pusher.BuildPayload(time.Now().UTC())
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(otlpBytes), `"luma_eye_events_total"`) {
		t.Fatalf("unexpected otlp payload: %s", string(otlpBytes))
	}
}
