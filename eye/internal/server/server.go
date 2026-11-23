package server

import (
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/url"
	"strings"

	"github.com/liutianjie/luma-eye/internal/auth"
	"github.com/liutianjie/luma-eye/internal/clickhouse"
	"github.com/liutianjie/luma-eye/internal/config"
	"github.com/liutianjie/luma-eye/internal/observe"
	"github.com/liutianjie/luma-eye/internal/pipeline"
	"github.com/liutianjie/luma-eye/internal/project"
	"github.com/liutianjie/luma-eye/internal/ratelimit"
	"github.com/liutianjie/luma-eye/internal/retention"
	"github.com/liutianjie/luma-eye/internal/sourcemap"
)

const sessionCookieName = "luma_eye_session"

type Server struct {
	cfg             *config.Config
	store           *project.Store
	ch              *clickhouse.Client
	batcher         *pipeline.Batcher
	symbolicator    *sourcemap.Symbolicator
	metrics         *observe.Metrics
	retentionStatus func() retention.Status
	auth            *auth.Manager
	controlSSO      *auth.ControlClient

	ipLimiter   *ratelimit.TokenLimiter
	projLimiter *ratelimit.TokenLimiter
	authLimiter *ratelimit.TokenLimiter

	mux *http.ServeMux
}

func (s *Server) SetRetentionProvider(fn func() retention.Status) {
	s.retentionStatus = fn
}

func New(
	cfg *config.Config,
	store *project.Store,
	ch *clickhouse.Client,
	batcher *pipeline.Batcher,
	symbolicator *sourcemap.Symbolicator,
	metrics *observe.Metrics,
) (*Server, error) {
	var dataDir string
	var controlClient *auth.ControlClient
	if cfg != nil {
		dataDir = cfg.DataDir
		if strings.TrimSpace(cfg.ControlURL) != "" {
			c, err := auth.NewControlClient(cfg.ControlURL, cfg.AddonID, cfg.ControlInsecure, cfg.ControlAuthCacheTTL)
			if err != nil {
				log.Printf("[luma-eye] ERROR: failed to initialize Luma Control SSO client (control_url=%q): %v", cfg.ControlURL, err)
				return nil, fmt.Errorf("init Luma Control SSO client: %w", err)
			}
			controlClient = c
		}
	}
	s := &Server{
		cfg:          cfg,
		store:        store,
		ch:           ch,
		batcher:      batcher,
		symbolicator: symbolicator,
		metrics:      metrics,
		auth:         initAuthManagerFallback(dataDir),
		controlSSO:   controlClient,
		ipLimiter:    ratelimit.NewTokenLimiter(60, 180),
		projLimiter:  ratelimit.NewTokenLimiter(200, 600),
		authLimiter:  ratelimit.NewTokenLimiter(1, 15),
		mux:          http.NewServeMux(),
	}
	s.registerRoutes()
	return s, nil
}

func (s *Server) Handler() http.Handler {
	return withGzip(s.mux)
}

func (s *Server) registerRoutes() {
	s.mux.HandleFunc("GET /healthz", s.handleHealthz)
	s.mux.HandleFunc("GET /metrics", s.handlePrometheusMetrics)

	// Public client ingestion (enabled in all and edge, and also accepted in core for local tests)
	s.mux.HandleFunc("OPTIONS /_e/b", s.handleCORS)
	s.mux.HandleFunc("OPTIONS /v1/b", s.handleCORS)
	s.mux.HandleFunc("POST /_e/b", s.handleUnifiedIngest)
	s.mux.HandleFunc("POST /v1/b", s.handleUnifiedIngest)

	s.mux.HandleFunc("OPTIONS /_e/r", s.handleCORS)
	s.mux.HandleFunc("OPTIONS /v1/r", s.handleCORS)
	s.mux.HandleFunc("POST /_e/r", s.handleReplayIngest)
	s.mux.HandleFunc("POST /v1/r", s.handleReplayIngest)

	s.mux.HandleFunc("OPTIONS /api/{id}/envelope/", s.handleCORS)
	s.mux.HandleFunc("OPTIONS /api/{id}/envelope", s.handleCORS)
	s.mux.HandleFunc("OPTIONS /_e/s/{id}", s.handleCORS)
	s.mux.HandleFunc("POST /api/{id}/envelope/", s.handleSentryEnvelope)
	s.mux.HandleFunc("POST /api/{id}/envelope", s.handleSentryEnvelope)
	s.mux.HandleFunc("POST /_e/s/{id}", s.handleSentryEnvelope)

	// Embedded JS SDK assets
	s.mux.HandleFunc("GET /_e/eye.js", s.handleSDKAsset)
	s.mux.HandleFunc("GET /_e/eye-replay.js", s.handleReplaySDKAsset)

	// Internal Edge -> Core relay endpoints
	if s.cfg.Mode != config.ModeEdge {
		s.mux.HandleFunc("POST /internal/v1/relay", s.handleRelayIngest)
		s.mux.HandleFunc("GET /internal/v1/projects-sync", s.handleProjectsSync)

		// Unified Luma Control Browser SSO handoff endpoints
		s.mux.HandleFunc("GET /auth/login", s.handleSSOLoginStart)
		s.mux.HandleFunc("GET /auth/callback", s.handleSSOCallback)

		// Auth, Password, Passkey (WebAuthn), & Browser Session API
		s.mux.HandleFunc("POST /api/v1/auth/login", s.handleLogin)
		s.mux.HandleFunc("POST /api/v1/auth/logout", s.handleLogout)
		s.mux.HandleFunc("GET /api/v1/auth/status", s.handleAuthStatus)
		s.mux.HandleFunc("POST /api/v1/auth/password", s.requireAdmin(s.handleUpdatePassword))
		s.mux.HandleFunc("PUT /api/v1/auth/profile", s.requireAdmin(s.handleUpdateProfile))
		s.mux.HandleFunc("GET /api/v1/auth/sessions", s.requireAdmin(s.handleListAuthSessions))
		s.mux.HandleFunc("POST /api/v1/auth/sessions/revoke", s.requireAdmin(s.handleRevokeAuthSessions))

		s.mux.HandleFunc("GET /api/v1/auth/passkeys", s.requireAdmin(s.handleListPasskeys))
		s.mux.HandleFunc("POST /api/v1/auth/passkeys/register/begin", s.requireAdmin(s.handlePasskeyRegisterBegin))
		s.mux.HandleFunc("POST /api/v1/auth/passkeys/register/finish", s.requireAdmin(s.handlePasskeyRegisterFinish))
		s.mux.HandleFunc("PUT /api/v1/auth/passkeys/{id}", s.requireAdmin(s.handleRenamePasskey))
		s.mux.HandleFunc("DELETE /api/v1/auth/passkeys/{id}", s.requireAdmin(s.handleDeletePasskey))
		s.mux.HandleFunc("POST /api/v1/auth/passkeys/login/begin", s.handlePasskeyLoginBegin)
		s.mux.HandleFunc("POST /api/v1/auth/passkeys/login/finish", s.handlePasskeyLoginFinish)

		s.mux.HandleFunc("GET /api/v1/projects", s.requireAdmin(s.handleListProjects))
		s.mux.HandleFunc("POST /api/v1/projects", s.requireAdmin(s.handleEnsureProject))
		s.mux.HandleFunc("DELETE /api/v1/projects/{slug}", s.requireAdmin(s.handleDeleteProject))

		s.mux.HandleFunc("GET /api/v1/query/portfolio", s.requireAdmin(s.handleQueryPortfolio))
		s.mux.HandleFunc("GET /api/v1/query/overview", s.requireAdmin(s.handleQueryOverview))
		s.mux.HandleFunc("GET /api/v1/query/pages", s.requireAdmin(s.handleQueryPages))
		s.mux.HandleFunc("POST /api/v1/query/funnel", s.requireAdmin(s.handleQueryFunnel))
		s.mux.HandleFunc("GET /api/v1/query/retention", s.requireAdmin(s.handleQueryRetention))

		s.mux.HandleFunc("GET /api/v1/events", s.requireAdmin(s.handleListEvents))
		s.mux.HandleFunc("GET /api/v1/sessions", s.requireAdmin(s.handleListSessions))
		s.mux.HandleFunc("GET /api/v1/timeline", s.requireAdmin(s.handleGetTimeline))
		s.mux.HandleFunc("GET /api/v1/replays/{sessionId}", s.requireAdmin(s.handleGetReplay))

		s.mux.HandleFunc("GET /api/v1/issues", s.requireAdmin(s.handleListIssues))
		s.mux.HandleFunc("GET /api/v1/issues/{fingerprint}", s.requireAdmin(s.handleIssueDetail))
		s.mux.HandleFunc("POST /api/v1/issues/{fingerprint}/status", s.requireAdmin(s.handleSetIssueStatus))

		s.mux.HandleFunc("GET /api/v1/logs", s.requireAdmin(s.handleSearchLogs))
		s.mux.HandleFunc("POST /api/v1/sourcemaps", s.requireAdmin(s.handleUploadSourceMap))
		s.mux.HandleFunc("GET /api/v1/grafana-dashboard", s.requireAdmin(s.handleGrafanaDashboard))

		// Embedded Web Console SPA
		s.mux.HandleFunc("GET /console", func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, "/console/", http.StatusMovedPermanently)
		})
		s.mux.HandleFunc("GET /console/", s.handleConsoleSPA)
		s.mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, "/console/", http.StatusFound)
		})
	} else {
		s.mux.HandleFunc("GET /{$}", s.handleHealthz)
	}
}

func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":   true,
		"mode": string(s.cfg.Mode),
	})
}

func (s *Server) handlePrometheusMetrics(w http.ResponseWriter, r *http.Request) {
	if !isLoopbackRequest(r) && !s.isAdminAuthorized(r) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	_, _ = w.Write([]byte(s.metrics.RenderPrometheus()))
}

func (s *Server) authorizeRequestInfo(w http.ResponseWriter, r *http.Request) (bool, string, int) {
	if tok := extractBearer(r); tok != "" {
		if s.cfg != nil && s.cfg.AdminToken != "" && subtle.ConstantTimeCompare([]byte(tok), []byte(s.cfg.AdminToken)) == 1 {
			return true, "token", http.StatusOK
		}
		if s.controlSSO != nil {
			if ok, err := s.controlSSO.VerifyToken(r.Context(), tok); err == nil && ok {
				return true, "control-token", http.StatusOK
			}
		}
		return false, "", http.StatusUnauthorized
	}
	if s.controlSSO != nil && readSSOSessionCookie(r) != "" {
		if _, status := s.verifyControlSSOCookie(r); status == http.StatusOK {
			return true, "control-sso", http.StatusOK
		} else if status == http.StatusForbidden {
			return false, "", http.StatusForbidden
		}
	}
	if c, err := r.Cookie(sessionCookieName); err == nil && c.Value != "" {
		rec, renewed, ok := s.auth.VerifySession(c.Value)
		if ok {
			// When Control SSO is enabled, only allow break-glass emergency local AdminToken sessions
			// (never pre-SSO local password/passkey or unrevocable control-token cookies).
			if s.controlSSO != nil && rec.Method != "token" {
				return false, "", http.StatusUnauthorized
			}
			if !s.verifyLocalSessionCSRF(r) {
				return false, "", http.StatusForbidden
			}
			if renewed != "" && w != nil {
				setSessionCookie(w, r, renewed)
			}
			method := "local"
			if rec.Method != "" {
				method = rec.Method
			}
			return true, method, http.StatusOK
		}
	}
	return false, "", http.StatusUnauthorized
}

func (s *Server) verifyLocalSessionCSRF(r *http.Request) bool {
	if isSafeHTTPMethod(r.Method) {
		return true
	}
	if sfs := strings.ToLower(strings.TrimSpace(r.Header.Get("Sec-Fetch-Site"))); sfs != "" {
		if sfs != "same-origin" && sfs != "none" {
			return false
		}
	}
	expectedOrigin := strings.TrimRight(s.resolvePublicURL(r), "/")
	origin := strings.TrimRight(strings.TrimSpace(r.Header.Get("Origin")), "/")
	if origin != "" {
		u, err := url.Parse(origin)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
			return false
		}
		if expectedOrigin != "" && strings.EqualFold(origin, expectedOrigin) {
			return true
		}
		if s.cfg == nil || s.cfg.PublicURL == "" {
			if strings.EqualFold(u.Host, s.requestHost(r)) {
				return true
			}
		}
		return false
	}
	if strings.EqualFold(strings.TrimSpace(r.Header.Get("Sec-Fetch-Site")), "same-origin") {
		return true
	}
	if ref := strings.TrimSpace(r.Header.Get("Referer")); ref != "" {
		if ru, err := url.Parse(ref); err == nil && (ru.Scheme == "http" || ru.Scheme == "https") && ru.Host != "" {
			refOrigin := ru.Scheme + "://" + ru.Host
			if expectedOrigin != "" && strings.EqualFold(refOrigin, expectedOrigin) {
				return true
			}
		}
	}
	return false
}

func (s *Server) authorizeRequest(w http.ResponseWriter, r *http.Request) bool {
	ok, _, _ := s.authorizeRequestInfo(w, r)
	return ok
}

func (s *Server) isAdminAuthorized(r *http.Request) bool {
	return s.authorizeRequest(nil, r)
}

func (s *Server) requireAdmin(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ok, _, status := s.authorizeRequestInfo(w, r)
		if !ok {
			if status == http.StatusForbidden {
				writeJSON(w, http.StatusForbidden, map[string]string{"error": "request origin does not match Luma Eye console"})
				return
			}
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
			return
		}
		next(w, r)
	}
}

func extractBearer(r *http.Request) string {
	authHeader := strings.TrimSpace(r.Header.Get("Authorization"))
	if len(authHeader) > 7 && strings.EqualFold(authHeader[:7], "bearer ") {
		return strings.TrimSpace(authHeader[7:])
	}
	return ""
}

func isLoopbackRequest(r *http.Request) bool {
	if r == nil {
		return false
	}
	for _, hdr := range []string{"X-Forwarded-For", "Ali-Real-Client-IP", "CF-Connecting-IP", "X-Real-IP", "Forwarded"} {
		if strings.TrimSpace(r.Header.Get(hdr)) != "" {
			return false
		}
	}
	host, _, err := net.SplitHostPort(strings.TrimSpace(r.RemoteAddr))
	if err != nil {
		host = strings.TrimSpace(r.RemoteAddr)
	}
	host = strings.Trim(host, "[]")
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
}

func setIngestCORS(w http.ResponseWriter, r *http.Request) {
	origin := strings.TrimSpace(r.Header.Get("Origin"))
	if origin != "" {
		w.Header().Set("Access-Control-Allow-Origin", origin)
		w.Header().Set("Vary", "Origin")
	} else {
		w.Header().Set("Access-Control-Allow-Origin", "*")
	}
	w.Header().Set("Access-Control-Allow-Methods", "POST, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Sentry-Auth")
	w.Header().Set("Access-Control-Allow-Credentials", "false")
	w.Header().Set("Access-Control-Max-Age", "86400")
}
