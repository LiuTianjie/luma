package server

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/liutianjie/luma-eye/internal/config"
	"github.com/liutianjie/luma-eye/internal/model"
	"github.com/liutianjie/luma-eye/internal/observe"
	"github.com/liutianjie/luma-eye/internal/pipeline"
	"github.com/liutianjie/luma-eye/internal/project"
	"github.com/liutianjie/luma-eye/internal/ratelimit"
	"github.com/liutianjie/luma-eye/internal/sourcemap"
)

type failOnReadBody struct {
	readCalled bool
}

func (f *failOnReadBody) Read(_ []byte) (int, error) {
	f.readCalled = true
	return 0, errors.New("body should not be read when IP rate-limited")
}

func (f *failOnReadBody) Close() error { return nil }

func TestPreDecodeIPRateLimitRejectsBeforeBodyRead(t *testing.T) {
	dir := t.TempDir()
	srv := newTestAuthServer(t, dir)
	// Restrict IP limiter to 1 burst token so the 2nd request from same IP is rejected immediately.
	srv.ipLimiter = ratelimit.NewTokenLimiter(0.001, 1)

	// Consume the 1 allowed token from 198.51.100.7
	firstReq := httptest.NewRequest(http.MethodPost, "/_e/b", strings.NewReader(`{}`))
	firstReq.RemoteAddr = "198.51.100.7:50000"
	firstReq.Header.Set("User-Agent", "Mozilla/5.0 Chrome/131.0")
	wFirst := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wFirst, firstReq)

	endpoints := []string{
		"/_e/b",
		"/api/1/envelope/",
		"/_e/r",
	}
	for _, ep := range endpoints {
		body := &failOnReadBody{}
		req := httptest.NewRequest(http.MethodPost, ep, body)
		req.RemoteAddr = "198.51.100.7:50001"
		req.Header.Set("User-Agent", "Mozilla/5.0 Chrome/131.0")
		w := httptest.NewRecorder()
		srv.Handler().ServeHTTP(w, req)
		if w.Code != http.StatusTooManyRequests {
			t.Fatalf("%s: expected 429 before body decode, got %d", ep, w.Code)
		}
		if body.readCalled {
			t.Fatalf("%s: request body was read despite IP rate limit rejection", ep)
		}
	}
}

func TestMetricsLoopbackAuthRejectsForwardedAndSpoofedHeaders(t *testing.T) {
	dir := t.TempDir()
	srv := newTestAuthServer(t, dir)

	// 1. Direct IPv4 loopback without forwarded headers -> allowed (200)
	rLocal4 := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	rLocal4.RemoteAddr = "127.0.0.1:41234"
	wLocal4 := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wLocal4, rLocal4)
	if wLocal4.Code != http.StatusOK {
		t.Fatalf("expected 200 for direct IPv4 loopback, got %d", wLocal4.Code)
	}

	// 2. Direct IPv6 loopback without forwarded headers -> allowed (200)
	rLocal6 := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	rLocal6.RemoteAddr = "[::1]:41234"
	wLocal6 := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wLocal6, rLocal6)
	if wLocal6.Code != http.StatusOK {
		t.Fatalf("expected 200 for direct IPv6 loopback, got %d", wLocal6.Code)
	}

	// 3. Any forwarded header on loopback RemoteAddr (e.g. Traefik proxy) MUST require auth -> 401
	for _, hdr := range []string{"X-Forwarded-For", "Ali-Real-Client-IP", "CF-Connecting-IP", "X-Real-IP", "Forwarded"} {
		rProxied := httptest.NewRequest(http.MethodGet, "/metrics", nil)
		rProxied.RemoteAddr = "127.0.0.1:41234"
		rProxied.Header.Set(hdr, "127.0.0.1")
		wProxied := httptest.NewRecorder()
		srv.Handler().ServeHTTP(wProxied, rProxied)
		if wProxied.Code != http.StatusUnauthorized {
			t.Fatalf("header %s on loopback RemoteAddr: expected 401, got %d", hdr, wProxied.Code)
		}
	}

	// 4. External RemoteAddr spoofing X-Forwarded-For: 127.0.0.1 -> 401
	rSpoof := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	rSpoof.RemoteAddr = "198.51.100.20:54321"
	rSpoof.Header.Set("X-Forwarded-For", "127.0.0.1")
	wSpoof := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wSpoof, rSpoof)
	if wSpoof.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 for spoofed X-Forwarded-For, got %d", wSpoof.Code)
	}

	// 5. Proxied request WITH valid admin Bearer token -> 200
	rAuth := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	rAuth.RemoteAddr = "127.0.0.1:41234"
	rAuth.Header.Set("X-Forwarded-For", "198.51.100.20")
	rAuth.Header.Set("Authorization", "Bearer bootstrap-admin-token-123456")
	wAuth := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wAuth, rAuth)
	if wAuth.Code != http.StatusOK {
		t.Fatalf("expected 200 for authorized proxied /metrics, got %d", wAuth.Code)
	}
}

func TestIngestBackpressureReturns503WithRetryAfter(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{
		Mode:          config.ModeAll,
		ListenAddr:    ":0",
		DataDir:       dir,
		AdminToken:    "admin-secret",
		RelaySecret:   "relay-secret",
		IPSalt:        "salt",
		QueueCapacity: 1, // tiny queue; batcher intentionally not started so queue stays full
		BatchSize:     10,
	}
	store, err := project.NewStore(dir, false)
	if err != nil {
		t.Fatal(err)
	}
	proj, _, err := store.EnsureProject("demo", "Demo", nil, 200)
	if err != nil {
		t.Fatal(err)
	}
	sym, err := sourcemap.NewSymbolicator(dir, 16)
	if err != nil {
		t.Fatal(err)
	}
	metrics := observe.NewMetrics()
	batcher, err := pipeline.NewBatcher(cfg, store, nil, sym, nil, metrics)
	if err != nil {
		t.Fatal(err)
	}
	// Saturate the 1-slot queue
	if !batcher.Enqueue(model.RelayBatch{Events: []model.Event{{Project: "demo", EventName: "fill"}}}) {
		t.Fatalf("expected initial enqueue to succeed")
	}

	srv, err := New(cfg, store, nil, batcher, sym, metrics)
	if err != nil {
		t.Fatal(err)
	}

	// 1. Unified ingest /_e/b
	uBody, _ := json.Marshal(map[string]any{
		"key":     proj.PublicKey,
		"project": "demo",
		"events":  []map[string]any{{"type": "pageview", "name": "pageview"}},
	})
	rUnified := httptest.NewRequest(http.MethodPost, "/_e/b", bytes.NewReader(uBody))
	rUnified.Header.Set("User-Agent", "Mozilla/5.0 Chrome/131.0")
	wUnified := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wUnified, rUnified)
	if wUnified.Code != http.StatusServiceUnavailable || wUnified.Header().Get("Retry-After") != "5" {
		t.Fatalf("/_e/b: expected 503 + Retry-After=5, got status=%d retry=%q", wUnified.Code, wUnified.Header().Get("Retry-After"))
	}

	// 2. Sentry envelope /api/1/envelope/
	envBody := "{\"event_id\":\"e1\"}\n{\"type\":\"event\"}\n{\"event_id\":\"e1\",\"exception\":{\"values\":[{\"type\":\"Error\",\"value\":\"boom\"}]}}\n"
	rSentry := httptest.NewRequest(http.MethodPost, "/api/1/envelope/?sentry_key="+proj.PublicKey, strings.NewReader(envBody))
	wSentry := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wSentry, rSentry)
	if wSentry.Code != http.StatusServiceUnavailable || wSentry.Header().Get("Retry-After") != "5" {
		t.Fatalf("/api/1/envelope/: expected 503 + Retry-After=5, got status=%d retry=%q", wSentry.Code, wSentry.Header().Get("Retry-After"))
	}

	// 3. Replay ingest /_e/r
	repBody, _ := json.Marshal(map[string]any{
		"key":        proj.PublicKey,
		"project":    "demo",
		"session_id": "ses_1",
		"events":     []map[string]any{{"t": 1, "ts": time.Now().UnixMilli()}},
	})
	rReplay := httptest.NewRequest(http.MethodPost, "/_e/r", bytes.NewReader(repBody))
	rReplay.Header.Set("User-Agent", "Mozilla/5.0 Chrome/131.0")
	wReplay := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wReplay, rReplay)
	if wReplay.Code != http.StatusServiceUnavailable || wReplay.Header().Get("Retry-After") != "5" {
		t.Fatalf("/_e/r: expected 503 + Retry-After=5, got status=%d retry=%q", wReplay.Code, wReplay.Header().Get("Retry-After"))
	}

	// 4. Internal relay /internal/v1/relay
	relBody, _ := json.Marshal(model.RelayBatch{Events: []model.Event{{Project: "demo", EventName: "relayed"}}})
	rRelay := httptest.NewRequest(http.MethodPost, "/internal/v1/relay", bytes.NewReader(relBody))
	rRelay.Header.Set("Authorization", "Bearer relay-secret")
	wRelay := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wRelay, rRelay)
	if wRelay.Code != http.StatusServiceUnavailable || wRelay.Header().Get("Retry-After") != "5" {
		t.Fatalf("/internal/v1/relay: expected 503 + Retry-After=5, got status=%d retry=%q", wRelay.Code, wRelay.Header().Get("Retry-After"))
	}
}

func TestNewFailsClosedOnInvalidControlConfig(t *testing.T) {
	dir := t.TempDir()
	store, err := project.NewStore(dir, false)
	if err != nil {
		t.Fatal(err)
	}

	for _, tc := range []struct {
		name    string
		url     string
		addonID string
	}{
		{name: "bad scheme", url: "ftp://control.example.com", addonID: "eye"},
		{name: "missing host", url: "https://", addonID: "eye"},
		{name: "invalid addon id", url: "https://control.example.com", addonID: "INVALID_ADDON!"},
	} {
		cfg := &config.Config{
			Mode:       config.ModeAll,
			DataDir:    dir,
			ControlURL: tc.url,
			AddonID:    tc.addonID,
		}
		srv, err := New(cfg, store, nil, nil, nil, observe.NewMetrics())
		if err == nil || srv != nil {
			t.Fatalf("%s: expected fail-closed error from server.New, got srv=%v err=%v", tc.name, srv, err)
		}
	}
}

func TestLocalSessionCSRFEnforcement(t *testing.T) {
	dir := t.TempDir()
	srv := newTestAuthServer(t, dir)

	// Login to obtain local luma_eye_session cookie
	loginBody, _ := json.Marshal(map[string]string{"token": "bootstrap-admin-token-123456"})
	reqLogin := httptest.NewRequest(http.MethodPost, "/api/v1/auth/login", bytes.NewReader(loginBody))
	wLogin := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wLogin, reqLogin)
	if wLogin.Code != http.StatusOK {
		t.Fatalf("login failed: %d", wLogin.Code)
	}
	var sessCookie *http.Cookie
	for _, c := range wLogin.Result().Cookies() {
		if c.Name == sessionCookieName {
			sessCookie = c
		}
	}
	if sessCookie == nil {
		t.Fatalf("expected session cookie")
	}

	// 1. Missing Origin / Sec-Fetch-Site on POST -> 403
	rNoOrig := httptest.NewRequest(http.MethodPost, "/api/v1/projects", strings.NewReader(`{"slug":"csrf1"}`))
	rNoOrig.AddCookie(sessCookie)
	wNoOrig := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wNoOrig, rNoOrig)
	if wNoOrig.Code != http.StatusForbidden {
		t.Fatalf("expected 403 for missing Origin on POST, got %d", wNoOrig.Code)
	}

	// 2. Mismatched Origin on POST -> 403
	rBadOrig := httptest.NewRequest(http.MethodPost, "/api/v1/projects", strings.NewReader(`{"slug":"csrf2"}`))
	rBadOrig.Header.Set("Origin", "https://evil.example.com")
	rBadOrig.AddCookie(sessCookie)
	wBadOrig := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wBadOrig, rBadOrig)
	if wBadOrig.Code != http.StatusForbidden {
		t.Fatalf("expected 403 for mismatched Origin, got %d", wBadOrig.Code)
	}

	// 3. Cross-site Sec-Fetch-Site even with matching Origin -> 403
	rCrossSite := httptest.NewRequest(http.MethodPost, "/api/v1/projects", strings.NewReader(`{"slug":"csrf3"}`))
	rCrossSite.Header.Set("Origin", "https://eye.itool.tech")
	rCrossSite.Header.Set("Sec-Fetch-Site", "cross-site")
	rCrossSite.AddCookie(sessCookie)
	wCrossSite := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wCrossSite, rCrossSite)
	if wCrossSite.Code != http.StatusForbidden {
		t.Fatalf("expected 403 for Sec-Fetch-Site: cross-site, got %d", wCrossSite.Code)
	}

	// 4. Matching Origin -> 201 Created
	rGood := httptest.NewRequest(http.MethodPost, "/api/v1/projects", strings.NewReader(`{"slug":"csrfgood"}`))
	rGood.Header.Set("Origin", "https://eye.itool.tech")
	rGood.Header.Set("Sec-Fetch-Site", "same-origin")
	rGood.AddCookie(sessCookie)
	wGood := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wGood, rGood)
	if wGood.Code != http.StatusCreated {
		t.Fatalf("expected 201 for matching Origin, got %d: %s", wGood.Code, wGood.Body.String())
	}

	// 5. Logout CSRF check: bad origin rejected (403) and session stays valid
	rBadLogout := httptest.NewRequest(http.MethodPost, "/api/v1/auth/logout", nil)
	rBadLogout.Header.Set("Origin", "https://evil.example.com")
	rBadLogout.AddCookie(sessCookie)
	wBadLogout := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wBadLogout, rBadLogout)
	if wBadLogout.Code != http.StatusForbidden {
		t.Fatalf("expected 403 for cross-origin logout, got %d", wBadLogout.Code)
	}

	rCheck := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	rCheck.AddCookie(sessCookie)
	wCheck := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wCheck, rCheck)
	if wCheck.Code != http.StatusOK {
		t.Fatalf("expected session to survive rejected CSRF logout, got %d", wCheck.Code)
	}
}

func TestControlSSORejectsControlTokenLocalCookieBypass(t *testing.T) {
	const controlToken = "luma_deploy_tok_revocable_12345"
	mockControl := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/auth/login/verify" && r.Header.Get("Authorization") == "Bearer "+controlToken {
			writeJSON(w, http.StatusOK, map[string]any{"clusterId": "luma-test"})
			return
		}
		http.Error(w, "unauthorized", http.StatusUnauthorized)
	}))
	defer mockControl.Close()

	dir := t.TempDir()
	store, err := project.NewStore(dir, false)
	if err != nil {
		t.Fatal(err)
	}
	cfg := &config.Config{
		Mode:                config.ModeAll,
		PublicURL:           "https://eye.example.com",
		DataDir:             dir,
		AdminToken:          "emergency-local-token",
		AdminTokenExplicit:  true,
		ControlURL:          mockControl.URL,
		ControlAuthCacheTTL: 30 * time.Second,
		AddonID:             "eye",
	}
	srv, err := New(cfg, store, nil, nil, nil, observe.NewMetrics())
	if err != nil {
		t.Fatal(err)
	}

	// 1. Submitting a Control token to /api/v1/auth/login must NOT mint a 30-day local cookie
	loginBody, _ := json.Marshal(map[string]string{"token": controlToken})
	reqLogin := httptest.NewRequest(http.MethodPost, "/api/v1/auth/login", bytes.NewReader(loginBody))
	wLogin := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wLogin, reqLogin)
	if wLogin.Code != http.StatusForbidden {
		t.Fatalf("expected 403 rejecting Control token exchange for 30d local cookie, got %d: %s", wLogin.Code, wLogin.Body.String())
	}
	for _, c := range wLogin.Result().Cookies() {
		if c.Name == sessionCookieName && c.Value != "" {
			t.Fatalf("must not issue local session cookie for Control token login")
		}
	}

	// 2. Pre-existing local password or control-token session cookies must NOT bypass Control SSO
	for _, method := range []string{"password", "passkey", "control-token"} {
		legacyCookie, _, err := srv.auth.CreateSession(method, "", "", "test-ua")
		if err != nil {
			t.Fatal(err)
		}
		req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
		req.AddCookie(&http.Cookie{Name: sessionCookieName, Value: legacyCookie})
		w := httptest.NewRecorder()
		srv.Handler().ServeHTTP(w, req)
		if w.Code != http.StatusUnauthorized {
			t.Fatalf("expected 401 rejecting legacy local %q cookie under Control SSO, got %d", method, w.Code)
		}
	}

	// 3. Local passkey login is disabled under Control SSO
	rPK := httptest.NewRequest(http.MethodPost, "/api/v1/auth/passkeys/login/begin", nil)
	wPK := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wPK, rPK)
	if wPK.Code != http.StatusForbidden {
		t.Fatalf("expected 403 for local passkey login under Control SSO, got %d", wPK.Code)
	}

	// 4. Break-glass emergency AdminToken login still works
	emBody, _ := json.Marshal(map[string]string{"token": "emergency-local-token"})
	rEm := httptest.NewRequest(http.MethodPost, "/api/v1/auth/login", bytes.NewReader(emBody))
	wEm := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wEm, rEm)
	if wEm.Code != http.StatusOK {
		t.Fatalf("expected break-glass AdminToken login to succeed, got %d", wEm.Code)
	}
}
