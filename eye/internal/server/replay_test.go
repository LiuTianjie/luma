package server

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestReplayAssetAndGzipSegmentIngest(t *testing.T) {
	dir := t.TempDir()
	srv := newTestAuthServer(t, dir)

	proj, _, err := srv.store.EnsureProject("demo-app", "DemoApp", []string{"https://demo-app.example.com"}, 0)
	if err != nil {
		t.Fatalf("EnsureProject: %v", err)
	}

	// 1. Verify /_e/eye-replay.js asset & ETag 304 behavior
	r1 := httptest.NewRequest(http.MethodGet, "/_e/eye-replay.js", nil)
	w1 := httptest.NewRecorder()
	srv.Handler().ServeHTTP(w1, r1)
	if w1.Code != http.StatusOK || w1.Body.Len() < 1000 {
		t.Fatalf("expected 200 with JS bundle, got %d len=%d", w1.Code, w1.Body.Len())
	}
	etag := w1.Header().Get("ETag")
	if etag == "" {
		t.Fatalf("expected ETag header on /_e/eye-replay.js")
	}

	r304 := httptest.NewRequest(http.MethodGet, "/_e/eye-replay.js", nil)
	r304.Header.Set("If-None-Match", etag)
	w304 := httptest.NewRecorder()
	srv.Handler().ServeHTTP(w304, r304)
	if w304.Code != http.StatusNotModified {
		t.Fatalf("expected 304 Not Modified, got %d", w304.Code)
	}

	// 2. Gzipped replay segment upload using CORS-simple text/plain without Content-Encoding header
	segPayload := map[string]any{
		"project":           "demo-app",
		"session_id":        "ses_replay_test_01",
		"actor_id":          "act_01",
		"seq":               0,
		"has_full_snapshot": 1,
		"page_url":          "https://demo-app.example.com/scan?token=secret999",
		"viewport_w":        1440,
		"viewport_h":        900,
		"events": []map[string]any{
			{"t": 1, "ts": 1700000000000, "href": "https://demo-app.example.com/scan", "w": 1440, "h": 900},
			{"t": 2, "ts": 1700000000010, "href": "https://demo-app.example.com/scan", "w": 1440, "h": 900, "root": map[string]any{"id": 1, "t": 0}},
			{"t": 5, "ts": 1700000001200, "x": 420, "y": 310, "tag": "button", "label": "Start Scan"},
		},
	}
	rawJSON, _ := json.Marshal(segPayload)
	var gzBuf bytes.Buffer
	gw := gzip.NewWriter(&gzBuf)
	_, _ = gw.Write(rawJSON)
	_ = gw.Close()

	rIngest := httptest.NewRequest(
		http.MethodPost,
		"/_e/r?k="+proj.PublicKey+"&p=demo-app&s=ses_replay_test_01&seq=0",
		bytes.NewReader(gzBuf.Bytes()),
	)
	rIngest.Header.Set("Origin", "https://demo-app.example.com")
	rIngest.Header.Set("Content-Type", "text/plain")
	rIngest.Header.Set("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/134.0.0.0")
	wIngest := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wIngest, rIngest)
	if wIngest.Code != http.StatusAccepted {
		t.Fatalf("expected 202 Accepted on gzip replay upload, got %d: %s", wIngest.Code, wIngest.Body.String())
	}

	// 3. Reject invalid key
	rBadKey := httptest.NewRequest(
		http.MethodPost,
		"/_e/r?k=pk_invalid&p=demo-app&s=ses_replay_test_01",
		bytes.NewReader(rawJSON),
	)
	rBadKey.Header.Set("User-Agent", "Mozilla/5.0 Chrome/134.0")
	wBadKey := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wBadKey, rBadKey)
	if wBadKey.Code != http.StatusUnauthorized && wBadKey.Code != http.StatusForbidden {
		t.Fatalf("expected 401/403 for invalid key, got %d", wBadKey.Code)
	}

	// 4. Verify /api/v1/replays/{sessionId} requires admin auth
	rUnauth := httptest.NewRequest(http.MethodGet, "/api/v1/replays/ses_replay_test_01?project=demo-app", nil)
	wUnauth := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wUnauth, rUnauth)
	if wUnauth.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 for unauthenticated replay query, got %d", wUnauth.Code)
	}

	rAdmin := httptest.NewRequest(http.MethodGet, "/api/v1/replays/ses_replay_test_01?project=demo-app", nil)
	rAdmin.Header.Set("Authorization", "Bearer bootstrap-admin-token-123456")
	wAdmin := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wAdmin, rAdmin)
	if wAdmin.Code != http.StatusOK {
		t.Fatalf("expected 200 for admin replay query, got %d: %s", wAdmin.Code, wAdmin.Body.String())
	}
}
