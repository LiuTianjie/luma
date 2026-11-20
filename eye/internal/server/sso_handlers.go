package server

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/liutianjie/luma-eye/internal/auth"
)

const (
	ssoSessionCookieHTTPS = "__Host-luma-eye-session"
	ssoLoginCookieHTTPS   = "__Host-luma-eye-login"
	ssoSessionCookieHTTP  = "luma_eye_sso_session"
	ssoLoginCookieHTTP    = "luma_eye_sso_login"
)

type ssoLoginCookiePayload struct {
	Verifier string `json:"v"`
	Redirect string `json:"r,omitempty"`
}

func ssoSessionCookieName(r *http.Request) string {
	if isRequestHTTPS(r) {
		return ssoSessionCookieHTTPS
	}
	return ssoSessionCookieHTTP
}

func ssoLoginCookieName(r *http.Request) string {
	if isRequestHTTPS(r) {
		return ssoLoginCookieHTTPS
	}
	return ssoLoginCookieHTTP
}

func readSSOSessionCookie(r *http.Request) string {
	for _, name := range []string{ssoSessionCookieHTTPS, ssoSessionCookieHTTP} {
		if c, err := r.Cookie(name); err == nil && strings.TrimSpace(c.Value) != "" {
			return strings.TrimSpace(c.Value)
		}
	}
	return ""
}

func readSSOLoginCookie(r *http.Request) (verifier string, redirect string) {
	for _, name := range []string{ssoLoginCookieHTTPS, ssoLoginCookieHTTP} {
		c, err := r.Cookie(name)
		if err != nil || strings.TrimSpace(c.Value) == "" {
			continue
		}
		raw, err := base64.RawURLEncoding.DecodeString(strings.TrimSpace(c.Value))
		if err != nil {
			continue
		}
		var payload ssoLoginCookiePayload
		if err := json.Unmarshal(raw, &payload); err == nil && payload.Verifier != "" {
			return payload.Verifier, sanitizeConsoleRedirect(payload.Redirect)
		}
	}
	return "", ""
}

func setSSOLoginCookie(w http.ResponseWriter, r *http.Request, verifier, redirect string) {
	encoded, _ := json.Marshal(ssoLoginCookiePayload{
		Verifier: verifier,
		Redirect: sanitizeConsoleRedirect(redirect),
	})
	http.SetCookie(w, &http.Cookie{
		Name:     ssoLoginCookieName(r),
		Value:    base64.RawURLEncoding.EncodeToString(encoded),
		Path:     "/",
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
		Secure:   isRequestHTTPS(r),
		MaxAge:   300,
	})
}

func clearSSOLoginCookies(w http.ResponseWriter, r *http.Request) {
	https := isRequestHTTPS(r)
	for _, name := range []string{ssoLoginCookieHTTPS, ssoLoginCookieHTTP} {
		if name == ssoLoginCookieHTTPS && !https {
			continue
		}
		http.SetCookie(w, &http.Cookie{
			Name:     name,
			Value:    "",
			Path:     "/",
			HttpOnly: true,
			SameSite: http.SameSiteLaxMode,
			Secure:   https,
			MaxAge:   -1,
		})
	}
}

func setSSOSessionCookie(w http.ResponseWriter, r *http.Request, token string, expiresAt int64) {
	lifetime := int(expiresAt - time.Now().Unix())
	if lifetime < 1 {
		lifetime = 60
	}
	http.SetCookie(w, &http.Cookie{
		Name:     ssoSessionCookieName(r),
		Value:    token,
		Path:     "/",
		HttpOnly: true,
		SameSite: http.SameSiteStrictMode,
		Secure:   isRequestHTTPS(r),
		MaxAge:   lifetime,
		Expires:  time.Unix(expiresAt, 0),
	})
}

func clearSSOSessionCookies(w http.ResponseWriter, r *http.Request) {
	https := isRequestHTTPS(r)
	for _, name := range []string{ssoSessionCookieHTTPS, ssoSessionCookieHTTP} {
		if name == ssoSessionCookieHTTPS && !https {
			continue
		}
		http.SetCookie(w, &http.Cookie{
			Name:     name,
			Value:    "",
			Path:     "/",
			HttpOnly: true,
			SameSite: http.SameSiteStrictMode,
			Secure:   https,
			MaxAge:   -1,
		})
	}
}

func setSSOHeaders(w http.ResponseWriter) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("X-Content-Type-Options", "nosniff")
}

func sanitizeConsoleRedirect(raw string) string {
	clean := strings.TrimSpace(raw)
	if clean == "" || !strings.HasPrefix(clean, "/") || strings.HasPrefix(clean, "//") || strings.ContainsAny(clean, "\r\n\\") {
		return ""
	}
	// Strip leading /console if caller passed a full path so we never double-prefix
	if clean == "/console" || clean == "/console/" {
		return "/portfolio"
	}
	if strings.HasPrefix(clean, "/console/") {
		clean = strings.TrimPrefix(clean, "/console")
	}
	return clean
}

func (s *Server) requestHost(r *http.Request) string {
	if s.cfg != nil && s.cfg.PublicURL != "" {
		if u, err := url.Parse(s.cfg.PublicURL); err == nil && u.Host != "" {
			return strings.ToLower(u.Host)
		}
	}
	if fwd := strings.TrimSpace(r.Header.Get("X-Forwarded-Host")); fwd != "" {
		host, _, _ := strings.Cut(fwd, ",")
		if clean := strings.ToLower(strings.TrimSpace(host)); clean != "" {
			return clean
		}
	}
	return strings.ToLower(strings.TrimSpace(r.Host))
}

func (s *Server) audienceMatchesRequest(r *http.Request, audience string) bool {
	u, err := url.Parse(strings.TrimSpace(audience))
	if err != nil || u.Scheme != "https" || u.Host == "" {
		return false
	}
	return strings.EqualFold(u.Host, s.requestHost(r))
}

func isSafeHTTPMethod(method string) bool {
	switch method {
	case http.MethodGet, http.MethodHead, http.MethodOptions:
		return true
	default:
		return false
	}
}

func (s *Server) handleSSOLoginStart(w http.ResponseWriter, r *http.Request) {
	setSSOHeaders(w)
	if s.controlSSO == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "Luma Control SSO is not configured"})
		return
	}
	theme := strings.TrimSpace(r.URL.Query().Get("theme"))
	redirect := sanitizeConsoleRedirect(r.URL.Query().Get("redirect"))
	targetURL, verifier, err := s.controlSSO.StartLogin(theme)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to start Luma Control sign-in"})
		return
	}
	setSSOLoginCookie(w, r, verifier, redirect)
	http.Redirect(w, r, targetURL, http.StatusSeeOther)
}

func (s *Server) handleSSOCallback(w http.ResponseWriter, r *http.Request) {
	setSSOHeaders(w)
	clearSSOLoginCookies(w, r)
	if s.controlSSO == nil {
		http.Redirect(w, r, "/console/login?sso=failed", http.StatusSeeOther)
		return
	}
	code := strings.TrimSpace(r.URL.Query().Get("code"))
	verifier, savedRedirect := readSSOLoginCookie(r)
	if code == "" || verifier == "" {
		http.Redirect(w, r, "/console/login?sso=failed", http.StatusSeeOther)
		return
	}
	sess, err := s.controlSSO.ExchangeCode(r.Context(), code, verifier)
	if err != nil || sess == nil || !s.audienceMatchesRequest(r, sess.Audience) {
		http.Redirect(w, r, "/console/login?sso=failed", http.StatusSeeOther)
		return
	}
	setSSOSessionCookie(w, r, sess.Token, sess.ExpiresAt)
	dest := "/console/portfolio"
	if savedRedirect != "" && savedRedirect != "/login" {
		dest = "/console" + savedRedirect
	}
	http.Redirect(w, r, dest, http.StatusSeeOther)
}

func (s *Server) verifyControlSSOCookie(r *http.Request) (sess *auth.ControlSession, status int) {
	if s.controlSSO == nil {
		return nil, http.StatusUnauthorized
	}
	tok := readSSOSessionCookie(r)
	if tok == "" {
		return nil, http.StatusUnauthorized
	}
	info, err := s.controlSSO.VerifySession(r.Context(), tok)
	if err != nil || info == nil {
		return nil, http.StatusUnauthorized
	}
	if !s.audienceMatchesRequest(r, info.Audience) {
		return nil, http.StatusUnauthorized
	}
	if !isSafeHTTPMethod(r.Method) {
		origin := strings.TrimRight(strings.TrimSpace(r.Header.Get("Origin")), "/")
		if origin != strings.TrimRight(info.Audience, "/") {
			return nil, http.StatusForbidden
		}
	}
	return info, http.StatusOK
}
