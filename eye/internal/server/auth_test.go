package server

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/liutianjie/luma-eye/internal/config"
	"github.com/liutianjie/luma-eye/internal/observe"
	"github.com/liutianjie/luma-eye/internal/pipeline"
	"github.com/liutianjie/luma-eye/internal/project"
	"github.com/liutianjie/luma-eye/internal/sourcemap"
)

func newTestAuthServer(t *testing.T, dataDir string) *Server {
	t.Helper()
	cfg := &config.Config{
		Mode:          config.ModeAll,
		ListenAddr:    ":8090",
		PublicURL:     "https://eye.itool.tech",
		DataDir:       dataDir,
		AdminToken:    "bootstrap-admin-token-123456",
		IPSalt:        "test-salt",
		QueueCapacity: 64,
		BatchSize:     16,
	}
	store, err := project.NewStore(dataDir, false)
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	sym, err := sourcemap.NewSymbolicator(dataDir, 32)
	if err != nil {
		t.Fatalf("NewSymbolicator: %v", err)
	}
	metrics := observe.NewMetrics()
	batcher, err := pipeline.NewBatcher(cfg, store, nil, sym, nil, metrics)
	if err != nil {
		t.Fatalf("NewBatcher: %v", err)
	}
	srv, err := New(cfg, store, nil, batcher, sym, metrics)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return srv
}

func TestPersistentSessionAndCustomPasswordAcrossServerRestart(t *testing.T) {
	dir := t.TempDir()
	srv1 := newTestAuthServer(t, dir)

	// 1. Initial login with bootstrap AdminToken
	loginBody, _ := json.Marshal(map[string]string{
		"username": "admin",
		"password": "bootstrap-admin-token-123456",
	})
	req1 := httptest.NewRequest(http.MethodPost, "/api/v1/auth/login", bytes.NewReader(loginBody))
	req1.Header.Set("X-Forwarded-Proto", "https")
	w1 := httptest.NewRecorder()
	srv1.Handler().ServeHTTP(w1, req1)
	if w1.Code != http.StatusOK {
		t.Fatalf("expected 200 on token login, got %d: %s", w1.Code, w1.Body.String())
	}

	cookies := w1.Result().Cookies()
	var sessCookie *http.Cookie
	for _, c := range cookies {
		if c.Name == sessionCookieName {
			sessCookie = c
			break
		}
	}
	if sessCookie == nil {
		t.Fatalf("expected session cookie to be issued")
	}
	if sessCookie.SameSite != http.SameSiteLaxMode || sessCookie.MaxAge < 25*86400 {
		t.Fatalf("expected 30d SameSite=Lax cookie, got SameSite=%v MaxAge=%d", sessCookie.SameSite, sessCookie.MaxAge)
	}

	// 2. Set custom password using active browser session (no need to re-type AdminToken!)
	pwBody, _ := json.Marshal(map[string]string{
		"new_password": "CloudflareGradePassword!99",
	})
	reqPw := httptest.NewRequest(http.MethodPost, "/api/v1/auth/password", bytes.NewReader(pwBody))
	reqPw.Header.Set("Origin", "https://eye.itool.tech")
	reqPw.AddCookie(sessCookie)
	wPw := httptest.NewRecorder()
	srv1.Handler().ServeHTTP(wPw, reqPw)
	if wPw.Code != http.StatusOK {
		t.Fatalf("expected 200 setting initial password, got %d: %s", wPw.Code, wPw.Body.String())
	}

	// 3. Simulate container rollout / process restart: create brand new Server on same dataDir
	srv2 := newTestAuthServer(t, dir)

	// Existing cookie MUST still be authenticated on srv2!
	reqStatus := httptest.NewRequest(http.MethodGet, "/api/v1/auth/status", nil)
	reqStatus.AddCookie(sessCookie)
	wStatus := httptest.NewRecorder()
	srv2.Handler().ServeHTTP(wStatus, reqStatus)
	if wStatus.Code != http.StatusOK {
		t.Fatalf("expected 200 from auth/status, got %d", wStatus.Code)
	}
	var statusResp map[string]any
	_ = json.Unmarshal(wStatus.Body.Bytes(), &statusResp)
	if statusResp["authenticated"] != true || statusResp["has_password"] != true {
		t.Fatalf("expected authenticated=true & has_password=true after restart, got %#v", statusResp)
	}

	// 4. Login with new custom password on srv2
	pwLoginBody, _ := json.Marshal(map[string]string{
		"username": "admin",
		"password": "CloudflareGradePassword!99",
	})
	reqPwLogin := httptest.NewRequest(http.MethodPost, "/api/v1/auth/login", bytes.NewReader(pwLoginBody))
	wPwLogin := httptest.NewRecorder()
	srv2.Handler().ServeHTTP(wPwLogin, reqPwLogin)
	if wPwLogin.Code != http.StatusOK {
		t.Fatalf("expected 200 logging in with custom password, got %d: %s", wPwLogin.Code, wPwLogin.Body.String())
	}

	// 5. Emergency AdminToken still works too!
	tokLoginBody, _ := json.Marshal(map[string]string{
		"token": "bootstrap-admin-token-123456",
	})
	reqTokLogin := httptest.NewRequest(http.MethodPost, "/api/v1/auth/login", bytes.NewReader(tokLoginBody))
	wTokLogin := httptest.NewRecorder()
	srv2.Handler().ServeHTTP(wTokLogin, reqTokLogin)
	if wTokLogin.Code != http.StatusOK {
		t.Fatalf("expected emergency AdminToken login to succeed, got %d", wTokLogin.Code)
	}
}

func TestUnknownPasskeyReturnsHTTP404(t *testing.T) {
	dir := t.TempDir()
	srv := newTestAuthServer(t, dir)

	// Begin passkey login
	reqBegin := httptest.NewRequest(http.MethodPost, "/api/v1/auth/passkeys/login/begin", nil)
	wBegin := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wBegin, reqBegin)
	if wBegin.Code != http.StatusOK {
		t.Fatalf("expected 200 from passkeys/login/begin, got %d: %s", wBegin.Code, wBegin.Body.String())
	}
	var opts struct {
		Challenge string `json:"challenge"`
		RPID      string `json:"rpId"`
	}
	_ = json.Unmarshal(wBegin.Body.Bytes(), &opts)

	// Build minimal syntactically valid assertion JSON referencing an unknown credential ID
	credID := base64.RawURLEncoding.EncodeToString([]byte("unknown-cred-123"))
	clientData, _ := json.Marshal(map[string]any{
		"type":      "webauthn.get",
		"challenge": opts.Challenge,
		"origin":    "https://eye.itool.tech",
	})
	authData := make([]byte, 37)
	authData[32] = 0x01 // UP flag set
	fakeSig := []byte{0x30, 0x06, 0x02, 0x01, 0x01, 0x02, 0x01, 0x01}

	assertionPayload, _ := json.Marshal(map[string]any{
		"id":    credID,
		"rawId": credID,
		"type":  "public-key",
		"response": map[string]any{
			"clientDataJSON":    base64.RawURLEncoding.EncodeToString(clientData),
			"authenticatorData": base64.RawURLEncoding.EncodeToString(authData),
			"signature":         base64.RawURLEncoding.EncodeToString(fakeSig),
		},
	})

	reqFin := httptest.NewRequest(http.MethodPost, "/api/v1/auth/passkeys/login/finish", bytes.NewReader(assertionPayload))
	for _, c := range wBegin.Result().Cookies() {
		reqFin.AddCookie(c)
	}
	wFin := httptest.NewRecorder()
	srv.Handler().ServeHTTP(wFin, reqFin)
	if wFin.Code != http.StatusNotFound {
		t.Fatalf("expected explicit HTTP 404 for unknown passkey credential, got %d: %s", wFin.Code, wFin.Body.String())
	}
}
