package server

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/liutianjie/luma-eye/internal/config"
	"github.com/liutianjie/luma-eye/internal/observe"
	"github.com/liutianjie/luma-eye/internal/project"
)

func TestControlSSOEndToEndAndRevocation(t *testing.T) {
	const (
		eyeOrigin     = "https://eye.example.com"
		controlToken  = "luma_deploy_tok_super_secret_123456"
		validCode     = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" // 43 chars
		issuedSession = "las_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
	)

	var (
		mu              sync.Mutex
		savedChallenge  string
		sessionActive   bool
		verifyTokenHits atomic.Int32
		verifySessHits  atomic.Int32
		logoutCalled    atomic.Int32
	)

	mockControl := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/auth/login/verify":
			verifyTokenHits.Add(1)
			if r.Header.Get("Authorization") != "Bearer "+controlToken {
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{"clusterId": "luma-test", "endpoint": "https://luma.example.com"})
		case "/v1/auth/addon/eye/exchange":
			var body struct {
				Code     string `json:"code"`
				Verifier string `json:"verifier"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			sum := sha256.Sum256([]byte(body.Verifier))
			chal := base64.RawURLEncoding.EncodeToString(sum[:])
			mu.Lock()
			ok := body.Code == validCode && chal == savedChallenge
			if ok {
				sessionActive = true
				savedChallenge = ""
			}
			mu.Unlock()
			if !ok {
				http.Error(w, "invalid ticket", http.StatusUnauthorized)
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{
				"authenticated": true,
				"token":         issuedSession,
				"expiresAt":     time.Now().Add(time.Hour).Unix(),
				"audience":      eyeOrigin,
				"addonId":       "eye",
			})
		case "/v1/auth/addon/eye/verify":
			verifySessHits.Add(1)
			mu.Lock()
			active := sessionActive && r.Header.Get("Authorization") == "Bearer "+issuedSession
			mu.Unlock()
			if !active {
				http.Error(w, "expired", http.StatusUnauthorized)
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{
				"authenticated": true,
				"audience":      eyeOrigin,
				"expiresAt":     time.Now().Add(time.Hour).Unix(),
				"addonId":       "eye",
				"actor":         "sess-pub-1",
			})
		case "/v1/auth/addon/eye/logout":
			logoutCalled.Add(1)
			mu.Lock()
			sessionActive = false
			mu.Unlock()
			writeJSON(w, http.StatusOK, map[string]any{"signedOut": true})
		default:
			http.NotFound(w, r)
		}
	}))
	defer mockControl.Close()

	dir := t.TempDir()
	store, err := project.NewStore(dir, false)
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	cfg := &config.Config{
		Mode:                config.ModeAll,
		PublicURL:           eyeOrigin,
		DataDir:             dir,
		AdminToken:          "fallback_admin_token_xyz",
		AdminTokenExplicit:  true,
		ControlURL:          mockControl.URL,
		ControlAuthCacheTTL: 60 * time.Second,
		AddonID:             "eye",
	}
	srv, err := New(cfg, store, nil, nil, nil, observe.NewMetrics())
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	// 1. Auth status reports control-sso mode when unauthenticated
	req, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/v1/auth/status", nil)
	req.Header.Set("X-Forwarded-Proto", "https")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("status req: %v", err)
	}
	var st map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&st)
	resp.Body.Close()
	if st["authenticated"] != false || st["auth_mode"] != "control-sso" || st["sso_login_url"] != "/auth/login" {
		t.Fatalf("unexpected unauth status: %+v", st)
	}

	// 2. Start PKCE login
	noRedirectClient := &http.Client{
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	startReq, _ := http.NewRequest(http.MethodGet, ts.URL+"/auth/login?theme=dark&redirect=/projects/itool-tech/overview", nil)
	startReq.Header.Set("X-Forwarded-Proto", "https")
	startResp, err := noRedirectClient.Do(startReq)
	if err != nil {
		t.Fatalf("start login: %v", err)
	}
	startResp.Body.Close()
	if startResp.StatusCode != http.StatusSeeOther {
		t.Fatalf("expected 303, got %d", startResp.StatusCode)
	}
	loc, err := url.Parse(startResp.Header.Get("Location"))
	if err != nil {
		t.Fatalf("parse redirect: %v", err)
	}
	if loc.Path != "/dashboard/addon-login" || loc.Query().Get("addon") != "eye" || loc.Query().Get("theme") != "dark" {
		t.Fatalf("unexpected redirect location: %s", loc.String())
	}
	mu.Lock()
	savedChallenge = loc.Query().Get("challenge")
	mu.Unlock()
	if len(savedChallenge) != 43 {
		t.Fatalf("expected 43-char PKCE challenge, got %q", savedChallenge)
	}
	var loginCookie *http.Cookie
	for _, c := range startResp.Cookies() {
		if c.Name == ssoLoginCookieHTTPS {
			loginCookie = c
		}
	}
	if loginCookie == nil || !loginCookie.HttpOnly || !loginCookie.Secure {
		t.Fatalf("expected secure HttpOnly login cookie, got %+v", startResp.Cookies())
	}

	// 3. Complete callback -> receive __Host-luma-eye-session cookie & redirect back to intended path
	cbReq, _ := http.NewRequest(http.MethodGet, ts.URL+"/auth/callback?code="+validCode, nil)
	cbReq.Header.Set("X-Forwarded-Proto", "https")
	cbReq.AddCookie(loginCookie)
	cbResp, err := noRedirectClient.Do(cbReq)
	if err != nil {
		t.Fatalf("callback: %v", err)
	}
	cbResp.Body.Close()
	if cbResp.StatusCode != http.StatusSeeOther || cbResp.Header.Get("Location") != "/console/projects/itool-tech/overview" {
		t.Fatalf("unexpected callback redirect: status=%d loc=%q", cbResp.StatusCode, cbResp.Header.Get("Location"))
	}
	var sessCookie *http.Cookie
	for _, c := range cbResp.Cookies() {
		if c.Name == ssoSessionCookieHTTPS && c.Value != "" {
			sessCookie = c
		}
	}
	if sessCookie == nil || !sessCookie.HttpOnly || !sessCookie.Secure || sessCookie.SameSite != http.SameSiteStrictMode {
		t.Fatalf("expected Strict Secure HttpOnly SSO session cookie, got %+v", cbResp.Cookies())
	}

	// 4. Authenticated GET succeeds via Control SSO cookie
	projReq, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/v1/projects", nil)
	projReq.Header.Set("X-Forwarded-Proto", "https")
	projReq.AddCookie(sessCookie)
	projResp, err := http.DefaultClient.Do(projReq)
	if err != nil {
		t.Fatalf("projects get: %v", err)
	}
	projResp.Body.Close()
	if projResp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200 with SSO cookie, got %d", projResp.StatusCode)
	}

	// 5. CSRF Origin check on mutating POST (reject missing or mismatched Origin, allow matching Origin)
	badPost, _ := http.NewRequest(http.MethodPost, ts.URL+"/api/v1/projects", strings.NewReader(`{"slug":"demo"}`))
	badPost.Header.Set("X-Forwarded-Proto", "https")
	badPost.Header.Set("Origin", "https://evil.example.com")
	badPost.AddCookie(sessCookie)
	badPostResp, _ := http.DefaultClient.Do(badPost)
	badPostResp.Body.Close()
	if badPostResp.StatusCode != http.StatusForbidden {
		t.Fatalf("expected 403 for mismatched Origin, got %d", badPostResp.StatusCode)
	}

	goodPost, _ := http.NewRequest(http.MethodPost, ts.URL+"/api/v1/projects", strings.NewReader(`{"slug":"demo"}`))
	goodPost.Header.Set("X-Forwarded-Proto", "https")
	goodPost.Header.Set("Origin", eyeOrigin)
	goodPost.AddCookie(sessCookie)
	goodPostResp, _ := http.DefaultClient.Do(goodPost)
	goodPostResp.Body.Close()
	if goodPostResp.StatusCode != http.StatusCreated && goodPostResp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200/201 for matching Origin, got %d", goodPostResp.StatusCode)
	}

	// 6. Explicit wrong Bearer must NEVER fall back to valid SSO cookie
	wrongBearer, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/v1/projects", nil)
	wrongBearer.Header.Set("Authorization", "Bearer totally-wrong")
	wrongBearer.AddCookie(sessCookie)
	wrongBearerResp, _ := http.DefaultClient.Do(wrongBearer)
	wrongBearerResp.Body.Close()
	if wrongBearerResp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("expected 401 when wrong Bearer accompanies valid cookie, got %d", wrongBearerResp.StatusCode)
	}

	// 7. Parent revocation on Control takes effect immediately (uncached)
	mu.Lock()
	sessionActive = false
	mu.Unlock()
	revokedReq, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/v1/projects", nil)
	revokedReq.AddCookie(sessCookie)
	revokedResp, _ := http.DefaultClient.Do(revokedReq)
	revokedResp.Body.Close()
	if revokedResp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("expected 401 immediately after Control revocation, got %d", revokedResp.StatusCode)
	}
	if verifySessHits.Load() < 3 {
		t.Fatalf("expected uncached verify calls on Control, got %d", verifySessHits.Load())
	}

	// 8. Control Bearer token auth works and caches SHA-256 digest
	verifyTokenHits.Store(0)
	for i := 0; i < 3; i++ {
		tokReq, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/v1/projects", nil)
		tokReq.Header.Set("Authorization", "Bearer "+controlToken)
		tokResp, err := http.DefaultClient.Do(tokReq)
		if err != nil {
			t.Fatalf("control token req %d: %v", i, err)
		}
		tokResp.Body.Close()
		if tokResp.StatusCode != http.StatusOK {
			t.Fatalf("expected 200 for Control deploy token, got %d", tokResp.StatusCode)
		}
	}
	if verifyTokenHits.Load() != 1 {
		t.Fatalf("expected 1 cached Control verify call, got %d", verifyTokenHits.Load())
	}

	// 9. Logout calls Control logout endpoint
	mu.Lock()
	sessionActive = true
	mu.Unlock()
	loReq, _ := http.NewRequest(http.MethodPost, ts.URL+"/api/v1/auth/logout", nil)
	loReq.Header.Set("X-Forwarded-Proto", "https")
	loReq.Header.Set("Origin", eyeOrigin)
	loReq.AddCookie(sessCookie)
	loResp, _ := http.DefaultClient.Do(loReq)
	loResp.Body.Close()
	if loResp.StatusCode != http.StatusOK || logoutCalled.Load() != 1 {
		t.Fatalf("expected logout 200 and Control logout call, status=%d calls=%d", loResp.StatusCode, logoutCalled.Load())
	}
}
