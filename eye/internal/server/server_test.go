package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/liutianjie/luma-eye/internal/config"
	"github.com/liutianjie/luma-eye/internal/observe"
	"github.com/liutianjie/luma-eye/internal/pipeline"
	"github.com/liutianjie/luma-eye/internal/project"
	"github.com/liutianjie/luma-eye/internal/sourcemap"
)

func TestServerRoutesAuthAndIngest(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{
		Mode:                  config.ModeAll,
		ListenAddr:            ":0",
		DataDir:               dir,
		AdminToken:            "test-admin-secret",
		IPSalt:                "test-salt",
		RelaySecret:           "test-relay",
		AutoProvisionKey:      "auto-vibe-key",
		AllowedOriginSuffixes: []string{".itool.tech"},
		BatchInterval:         50 * time.Millisecond,
		BatchSize:             100,
		QueueCapacity:         256,
	}

	store, err := project.NewStore(dir, false)
	if err != nil {
		t.Fatal(err)
	}
	sym, err := sourcemap.NewSymbolicator(dir, 64)
	if err != nil {
		t.Fatal(err)
	}
	metrics := observe.NewMetrics()
	batcher, err := pipeline.NewBatcher(cfg, store, nil, sym, nil, metrics)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	batcher.Start(ctx)
	defer func() {
		cancel()
		shutCtx, sCancel := context.WithTimeout(context.Background(), time.Second)
		batcher.Shutdown(shutCtx)
		sCancel()
	}()

	srv, err := New(cfg, store, nil, batcher, sym, metrics)
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	// 1. Login -> HttpOnly session cookie
	loginReq, _ := http.NewRequest(http.MethodPost, ts.URL+"/api/v1/auth/login", strings.NewReader(`{"token":"test-admin-secret"}`))
	loginResp, err := http.DefaultClient.Do(loginReq)
	if err != nil || loginResp.StatusCode != http.StatusOK {
		t.Fatalf("login failed: resp=%+v err=%v", loginResp, err)
	}
	cookies := loginResp.Cookies()
	_ = loginResp.Body.Close()
	if len(cookies) == 0 || cookies[0].Name != sessionCookieName || !cookies[0].HttpOnly {
		t.Fatalf("expected HttpOnly session cookie, got %+v", cookies)
	}

	// 2. Create project using session cookie
	projReq, _ := http.NewRequest(http.MethodPost, ts.URL+"/api/v1/projects", strings.NewReader(`{"slug":"vibecheck","allowed_origins":["https://vibecheck.itool.tech"]}`))
	projReq.Header.Set("Origin", ts.URL)
	projReq.AddCookie(cookies[0])
	projResp, err := http.DefaultClient.Do(projReq)
	if err != nil || projResp.StatusCode != http.StatusCreated {
		t.Fatalf("create project failed: resp=%+v err=%v", projResp, err)
	}
	var projOut struct {
		Project struct {
			DSNID     int    `json:"dsn_id"`
			Slug      string `json:"slug"`
			PublicKey string `json:"public_key"`
		} `json:"project"`
	}
	_ = json.NewDecoder(projResp.Body).Decode(&projOut)
	_ = projResp.Body.Close()
	if projOut.Project.PublicKey == "" || projOut.Project.DSNID != 1 {
		t.Fatalf("unexpected created project: %+v", projOut)
	}

	// 3. Track 1 + Track 2 ingest via text/plain POST /_e/b (zero-preflight)
	ingestPayload := map[string]any{
		"key":       projOut.Project.PublicKey,
		"project":   "vibecheck",
		"track":     "urgent",
		"actor_id":  "u_1",
		"session_id": "s_1",
		"events": []map[string]any{
			{"type": "pageview", "name": "pageview", "page_path": "/", "duration_ms": 1500},
		},
		"errors": []map[string]any{
			{
				"type":    "TypeError",
				"message": "undefined click",
				"stack":   "TypeError: undefined click\n    at onClick (https://vibecheck.itool.tech/assets/app.js:1:100)",
				"breadcrumbs": []map[string]any{
					{"ts": time.Now().UnixMilli(), "category": "ui.click", "level": "info", "message": "button#go"},
				},
			},
		},
	}
	rawIngest, _ := json.Marshal(ingestPayload)
	ingReq, _ := http.NewRequest(http.MethodPost, ts.URL+"/_e/b", bytes.NewReader(rawIngest))
	ingReq.Header.Set("Content-Type", "text/plain;charset=UTF-8")
	ingReq.Header.Set("Origin", "https://vibecheck.itool.tech")
	ingResp, err := http.DefaultClient.Do(ingReq)
	if err != nil || ingResp.StatusCode != http.StatusNoContent {
		t.Fatalf("ingest failed: resp=%+v err=%v", ingResp, err)
	}
	_ = ingResp.Body.Close()

	// 4. Sentry Envelope ingest
	envBody := "{\"event_id\":\"e1\"}\n{\"type\":\"event\"}\n{\"event_id\":\"e1\",\"exception\":{\"values\":[{\"type\":\"Error\",\"value\":\"sentry test\"}]}}\n"
	sReq, _ := http.NewRequest(http.MethodPost, ts.URL+"/api/1/envelope/?sentry_key="+projOut.Project.PublicKey, strings.NewReader(envBody))
	sResp, err := http.DefaultClient.Do(sReq)
	if err != nil || sResp.StatusCode != http.StatusOK {
		t.Fatalf("sentry envelope failed: resp=%+v err=%v", sResp, err)
	}
	_ = sResp.Body.Close()

	// 5. Embedded /_e/eye.js and /console/
	sdkResp, err := http.Get(ts.URL + "/_e/eye.js")
	if err != nil || sdkResp.StatusCode != http.StatusOK {
		t.Fatalf("GET /_e/eye.js failed: %v", err)
	}
	_ = sdkResp.Body.Close()

	conResp, err := http.Get(ts.URL + "/console/")
	if err != nil || conResp.StatusCode != http.StatusOK {
		t.Fatalf("GET /console/ failed: %v", err)
	}
	_ = conResp.Body.Close()

	// Deep client-side routed URLs under /console/* must serve SPA index.html (HTTP 200)
	for _, deepPath := range []string{
		"/console/portfolio",
		"/console/projects/vibecheck/overview",
		"/console/projects/vibecheck/sessions/s_1",
		"/console/projects/vibecheck/issues/fp_123",
	} {
		dResp, dErr := http.Get(ts.URL + deepPath)
		if dErr != nil || dResp.StatusCode != http.StatusOK {
			t.Fatalf("GET %s failed: resp=%+v err=%v", deepPath, dResp, dErr)
		}
		_ = dResp.Body.Close()
	}

	// 6. Portfolio overview (works even when ClickHouse is nil in unit test mode)
	pfReq, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/v1/query/portfolio?since=24h", nil)
	pfReq.AddCookie(cookies[0])
	pfResp, err := http.DefaultClient.Do(pfReq)
	if err != nil || pfResp.StatusCode != http.StatusOK {
		t.Fatalf("GET /api/v1/query/portfolio failed: resp=%+v err=%v", pfResp, err)
	}
	var pfOut struct {
		TotalProjects int `json:"total_projects"`
	}
	_ = json.NewDecoder(pfResp.Body).Decode(&pfOut)
	_ = pfResp.Body.Close()
	if pfOut.TotalProjects != 1 {
		t.Fatalf("expected portfolio total_projects=1, got %+v", pfOut)
	}

	// 7. Timeline endpoint validates missing session_id & actor_id
	tlReq, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/v1/timeline?project=vibecheck", nil)
	tlReq.AddCookie(cookies[0])
	tlResp, err := http.DefaultClient.Do(tlReq)
	if err != nil || tlResp.StatusCode != http.StatusBadRequest {
		t.Fatalf("expected 400 when timeline lacks session_id and actor_id, got resp=%+v err=%v", tlResp, err)
	}
	_ = tlResp.Body.Close()

	// Wait briefly for batcher flush to update metrics
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		snap, _, _, _, _, _ := metrics.Snapshot()
		if snap["vibecheck"].Events >= 1 && snap["vibecheck"].Exceptions >= 2 {
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	snap, _, _, _, _, _ := metrics.Snapshot()
	t.Fatalf("expected metrics for vibecheck to record events & exceptions, got %+v", snap["vibecheck"])
}

func TestReactNativeIngestMetadataOverridesAndHermesStack(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{
		Mode:          config.ModeAll,
		ListenAddr:    ":0",
		DataDir:       dir,
		AdminToken:    "admin",
		IPSalt:        "salt",
		BatchInterval: 20 * time.Millisecond,
		BatchSize:     100,
		QueueCapacity: 64,
	}
	store, err := project.NewStore(dir, false)
	if err != nil {
		t.Fatal(err)
	}
	proj, _, err := store.EnsureProject("rnapp", "RN App", nil, 200)
	if err != nil {
		t.Fatal(err)
	}
	sym, _ := sourcemap.NewSymbolicator(dir, 32)
	metrics := observe.NewMetrics()
	batcher, err := pipeline.NewBatcher(cfg, store, nil, sym, nil, metrics)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	batcher.Start(ctx)
	defer func() {
		cancel()
		shutCtx, sCancel := context.WithTimeout(context.Background(), time.Second)
		batcher.Shutdown(shutCtx)
		sCancel()
	}()

	srv, err := New(cfg, store, nil, batcher, sym, metrics)
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	payload := map[string]any{
		"key":         proj.PublicKey,
		"project":     "rnapp",
		"track":       "normal",
		"sdk":         "luma-eye-rn/1.0.0",
		"os":          "iOS",
		"device_type": "mobile",
		"actor_id":    "rn_u1",
		"session_id":  "rn_s1",
		"events": []map[string]any{
			{"type": "pageview", "name": "pageview", "page_path": "/Checkout", "page_url": "app:///Checkout", "duration_ms": 2100},
		},
		"errors": []map[string]any{
			{
				"type":    "Error",
				"message": "hermes crash",
				"stack":   "Error: hermes crash\n    at buy (address at main.jsbundle:1:0)",
			},
		},
	}
	raw, _ := json.Marshal(payload)
	req, _ := http.NewRequest(http.MethodPost, ts.URL+"/_e/b", bytes.NewReader(raw))
	req.Header.Set("Content-Type", "text/plain")
	req.Header.Set("User-Agent", "okhttp/4.12.0")
	resp, err := http.DefaultClient.Do(req)
	if err != nil || resp.StatusCode != http.StatusNoContent {
		t.Fatalf("rn ingest failed: resp=%+v err=%v", resp, err)
	}
	_ = resp.Body.Close()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		snap, _, _, _, _, _ := metrics.Snapshot()
		if snap["rnapp"].Pageviews >= 1 && snap["rnapp"].Exceptions >= 1 {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	snap, _, _, _, _, _ := metrics.Snapshot()
	t.Fatalf("expected rnapp pageviews & exceptions, got %+v", snap["rnapp"])
}
