package server

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/liutianjie/luma-eye/internal/auth"
	"github.com/liutianjie/luma-eye/internal/retention"
	"github.com/liutianjie/luma-eye/internal/scrub"
)

const ceremonyCookieName = "luma_eye_wa_ceremony"

func isRequestHTTPS(r *http.Request) bool {
	if r.TLS != nil || strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https") {
		return true
	}
	return false
}

func setSessionCookie(w http.ResponseWriter, r *http.Request, token string) {
	http.SetCookie(w, &http.Cookie{
		Name:     sessionCookieName,
		Value:    token,
		Path:     "/",
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
		Secure:   isRequestHTTPS(r),
		MaxAge:   int(auth.SessionTTL / time.Second),
		Expires:  time.Now().Add(auth.SessionTTL),
	})
}

func clearSessionCookie(w http.ResponseWriter, r *http.Request) {
	http.SetCookie(w, &http.Cookie{
		Name:     sessionCookieName,
		Value:    "",
		Path:     "/",
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
		Secure:   isRequestHTTPS(r),
		MaxAge:   -1,
	})
}

func setCeremonyCookie(w http.ResponseWriter, r *http.Request, ceremonyID string) {
	http.SetCookie(w, &http.Cookie{
		Name:     ceremonyCookieName,
		Value:    ceremonyID,
		Path:     "/",
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
		Secure:   isRequestHTTPS(r),
		MaxAge:   300,
	})
}

func readCeremonyCookie(r *http.Request) string {
	if c, err := r.Cookie(ceremonyCookieName); err == nil {
		return strings.TrimSpace(c.Value)
	}
	return ""
}

func (s *Server) resolveRPInfo(r *http.Request) (origin string, rpID string, available bool) {
	pub := s.resolvePublicURL(r)
	orig, rp, err := auth.ResolveRPOriginAndID(pub)
	if err != nil {
		return "", "", false
	}
	return orig, rp, true
}

func (s *Server) handleAuthStatus(w http.ResponseWriter, r *http.Request) {
	authorized, authMethod, _ := s.authorizeRequestInfo(w, r)
	snap := s.auth.Snapshot()
	_, rpID, rpAvailable := s.resolveRPInfo(r)

	authMode := "local"
	controlURL := ""
	controlSecurityURL := ""
	ssoLoginURL := ""
	tokenLoginEnabled := true
	if s.controlSSO != nil {
		authMode = "control-sso"
		controlURL = s.controlSSO.Endpoint()
		controlSecurityURL = s.controlSSO.SecuritySettingsURL()
		ssoLoginURL = "/auth/login"
		tokenLoginEnabled = true
	}

	payload := map[string]any{
		"authenticated":        authorized,
		"auth_mode":            authMode,
		"control_url":          controlURL,
		"control_security_url": controlSecurityURL,
		"sso_login_url":        ssoLoginURL,
		"token_login_enabled":  tokenLoginEnabled,
		"mode":                 string(s.cfg.Mode),
		"username":             snap.Username,
		"has_password":         snap.HasPassword,
		"passkey_count":        snap.PasskeyCount,
		"passkeys_available":   rpAvailable && authMode == "local",
		"rp_id":                rpID,
		"session_ttl_days":     int(auth.SessionTTL / (24 * time.Hour)),
	}
	if authorized {
		payload["auth_method"] = authMethod
		payload["display_name"] = snap.DisplayName
		payload["user_id"] = snap.UserID
		if s.retentionStatus != nil {
			payload["retention"] = s.retentionStatus()
		} else if s.cfg != nil {
			payload["retention"] = retention.Status{
				RetentionDays:   s.cfg.RetentionDays,
				MaxStorageBytes: s.cfg.MaxStorageBytes,
			}
		}
	}
	writeJSON(w, http.StatusOK, payload)
}

func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
	clientIP := scrub.ExtractClientIP(r)
	if s.authLimiter != nil && !s.authLimiter.Allow(clientIP, 0) {
		writeJSON(w, http.StatusTooManyRequests, map[string]string{"error": "登录尝试过于频繁，请稍后再试"})
		return
	}

	var body struct {
		Username string `json:"username"`
		Password string `json:"password"`
		Token    string `json:"token"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return
	}

	rawToken := strings.TrimSpace(body.Token)
	rawPassword := body.Password
	candidateSecret := rawPassword
	if candidateSecret == "" {
		candidateSecret = rawToken
	}
	trimmedCandidate := strings.TrimSpace(candidateSecret)

	var loginMethod string
	if trimmedCandidate != "" && s.cfg.AdminToken != "" && subtle.ConstantTimeCompare([]byte(trimmedCandidate), []byte(s.cfg.AdminToken)) == 1 {
		loginMethod = "token"
	} else if trimmedCandidate != "" && s.controlSSO != nil {
		if ok, err := s.controlSSO.VerifyToken(r.Context(), trimmedCandidate); err == nil && ok {
			loginMethod = "control-token"
		}
	}
	if loginMethod == "" && rawPassword != "" && s.controlSSO == nil && s.auth.VerifyPassword(body.Username, rawPassword) {
		loginMethod = "password"
	}
	if loginMethod == "" {
		errMsg := "管理 Token 不正确"
		if s.controlSSO == nil && s.auth.HasPassword() {
			errMsg = "用户名、密码或管理 Token 不正确"
		}
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": errMsg})
		return
	}

	cookieVal, _, err := s.auth.CreateSession(loginMethod, "", "", r.UserAgent())
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to persist session"})
		return
	}
	setSessionCookie(w, r, cookieVal)

	snap := s.auth.Snapshot()
	writeJSON(w, http.StatusOK, map[string]any{
		"authenticated": true,
		"method":        loginMethod,
		"username":      snap.Username,
		"display_name":  snap.DisplayName,
		"has_password":  snap.HasPassword,
		"passkey_count": snap.PasskeyCount,
	})
}

func (s *Server) handleLogout(w http.ResponseWriter, r *http.Request) {
	if s.controlSSO != nil {
		if tok := readSSOSessionCookie(r); tok != "" {
			if info, status := s.verifyControlSSOCookie(r); status == http.StatusForbidden {
				writeJSON(w, http.StatusForbidden, map[string]string{"error": "request origin does not match Luma Eye console"})
				return
			} else if status == http.StatusOK && info != nil {
				_ = s.controlSSO.LogoutSession(r.Context(), tok)
			}
		}
		clearSSOSessionCookies(w, r)
		clearSSOLoginCookies(w, r)
	}
	if c, err := r.Cookie(sessionCookieName); err == nil && c.Value != "" {
		s.auth.RevokeSessionByCookie(c.Value)
	}
	clearSessionCookie(w, r)
	writeJSON(w, http.StatusOK, map[string]any{"authenticated": false})
}

func (s *Server) handleUpdatePassword(w http.ResponseWriter, r *http.Request) {
	var body struct {
		CurrentPassword string `json:"current_password"`
		NewPassword     string `json:"new_password"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return
	}

	// If a custom password is already configured, verify authorization to replace it:
	// allow if request used AdminToken Bearer header, OR if current browser session signed in via Passkey/Token,
	// OR if CurrentPassword matches either current custom password or AdminToken.
	if s.auth.HasPassword() && extractBearer(r) == "" {
		trustedSession := false
		if c, err := r.Cookie(sessionCookieName); err == nil && c.Value != "" {
			if rec, _, ok := s.auth.VerifySession(c.Value); ok && (rec.Method == "passkey" || rec.Method == "token") {
				trustedSession = true
			}
		}
		if !trustedSession {
			cur := body.CurrentPassword
			curTrim := strings.TrimSpace(cur)
			validCur := (curTrim != "" && subtle.ConstantTimeCompare([]byte(curTrim), []byte(s.cfg.AdminToken)) == 1) ||
				s.auth.VerifyPassword("", cur)
			if !validCur {
				writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "当前密码或管理 Token 验证未通过"})
				return
			}
		}
	}

	if err := s.auth.SetPassword(body.NewPassword); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	snap := s.auth.Snapshot()
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":           true,
		"username":     snap.Username,
		"has_password": snap.HasPassword,
	})
}

func (s *Server) handleUpdateProfile(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Name        string `json:"name"`
		DisplayName string `json:"displayName"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return
	}
	snap, err := s.auth.UpdateProfile(body.Name, body.DisplayName)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	_, rpID, _ := s.resolveRPInfo(r)
	writeJSON(w, http.StatusOK, map[string]any{
		"name":        snap.Username,
		"displayName": snap.DisplayName,
		"rpId":        rpID,
		"userId":      snap.UserID,
	})
}

func (s *Server) handleListPasskeys(w http.ResponseWriter, r *http.Request) {
	_, rpID, available := s.resolveRPInfo(r)
	userID, creds := s.auth.ListPasskeys()
	writeJSON(w, http.StatusOK, map[string]any{
		"rpId":        rpID,
		"available":   available,
		"userId":      userID,
		"credentials": creds,
	})
}

func (s *Server) handlePasskeyRegisterBegin(w http.ResponseWriter, r *http.Request) {
	origin, rpID, ok := s.resolveRPInfo(r)
	if !ok {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "当前访问地址不支持 WebAuthn（需使用 HTTPS 域名或 localhost）"})
		return
	}
	var body struct {
		Name      string `json:"name"`
		Promotion bool   `json:"promotion"`
	}
	_ = json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&body)

	opts, ceremonyID, err := s.auth.BeginRegistration(origin, rpID, body.Name, body.Promotion)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	setCeremonyCookie(w, r, ceremonyID)
	writeJSON(w, http.StatusOK, opts)
}

func (s *Server) handlePasskeyRegisterFinish(w http.ResponseWriter, r *http.Request) {
	raw, err := io.ReadAll(io.LimitReader(r.Body, 64<<10))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "read body failed"})
		return
	}
	pk, rpID, userID, err := s.auth.FinishRegistration(readCeremonyCookie(r), raw, r.UserAgent())
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"passkey": pk,
		"rpId":    rpID,
		"userId":  userID,
	})
}

func (s *Server) handleRenamePasskey(w http.ResponseWriter, r *http.Request) {
	credID := strings.TrimSpace(r.PathValue("id"))
	var body struct {
		Name string `json:"name"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return
	}
	updated, err := s.auth.RenamePasskey(credID, body.Name)
	if err != nil {
		if errors.Is(err, auth.ErrPasskeyNotFound) {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "Passkey 不存在"})
			return
		}
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

func (s *Server) handleDeletePasskey(w http.ResponseWriter, r *http.Request) {
	credID := strings.TrimSpace(r.PathValue("id"))
	signedOut, userID, err := s.auth.DeletePasskey(credID)
	if err != nil {
		if errors.Is(err, auth.ErrPasskeyNotFound) {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "Passkey 不存在"})
			return
		}
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	_, rpID, _ := s.resolveRPInfo(r)
	_, creds := s.auth.ListPasskeys()
	writeJSON(w, http.StatusOK, map[string]any{
		"deleted":           credID,
		"signedOutSessions": signedOut,
		"rpId":              rpID,
		"userId":            userID,
		"credentials":       creds,
	})
}

func (s *Server) handlePasskeyLoginBegin(w http.ResponseWriter, r *http.Request) {
	origin, rpID, ok := s.resolveRPInfo(r)
	if !ok {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "当前访问地址不支持 WebAuthn"})
		return
	}
	opts, ceremonyID, err := s.auth.BeginLogin(origin, rpID)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	setCeremonyCookie(w, r, ceremonyID)
	writeJSON(w, http.StatusOK, opts)
}

func (s *Server) handlePasskeyLoginFinish(w http.ResponseWriter, r *http.Request) {
	clientIP := scrub.ExtractClientIP(r)
	if s.authLimiter != nil && !s.authLimiter.Allow(clientIP, 0) {
		writeJSON(w, http.StatusTooManyRequests, map[string]string{"error": "登录尝试过于频繁，请稍后再试"})
		return
	}

	raw, err := io.ReadAll(io.LimitReader(r.Body, 64<<10))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "read body failed"})
		return
	}
	pk, err := s.auth.FinishLogin(readCeremonyCookie(r), raw)
	if err != nil {
		if errors.Is(err, auth.ErrPasskeyNotFound) {
			// Explicit HTTP 404 per WebAuthn Signal API spec so client invokes signalUnknownCredential.
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "该 Passkey 未在此控制台注册或已被移除"})
			return
		}
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": err.Error()})
		return
	}

	cookieVal, _, err := s.auth.CreateSession("passkey", pk.ID, pk.Name, r.UserAgent())
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to persist session"})
		return
	}
	setSessionCookie(w, r, cookieVal)

	snap := s.auth.Snapshot()
	writeJSON(w, http.StatusOK, map[string]any{
		"authenticated": true,
		"method":        "passkey",
		"passkey_name":  pk.Name,
		"username":      snap.Username,
		"display_name":  snap.DisplayName,
	})
}

func (s *Server) handleListAuthSessions(w http.ResponseWriter, r *http.Request) {
	curCookie := ""
	if c, err := r.Cookie(sessionCookieName); err == nil {
		curCookie = c.Value
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"sessions":        s.auth.ListSessions(curCookie),
		"sessionTtlHours": int(auth.SessionTTL / time.Hour),
	})
}

func (s *Server) handleRevokeAuthSessions(w http.ResponseWriter, r *http.Request) {
	var body struct {
		ID  string `json:"id"`
		All bool   `json:"all"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return
	}

	curCookie := ""
	if c, err := r.Cookie(sessionCookieName); err == nil {
		curCookie = c.Value
	}

	if body.ID != "" {
		removed, err := s.auth.RevokeSession(body.ID)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		// If user revoked their own current session, clear cookie too.
		if _, _, ok := s.auth.VerifySession(curCookie); !ok && curCookie != "" {
			clearSessionCookie(w, r)
		}
		writeJSON(w, http.StatusOK, map[string]any{"revoked": removed})
		return
	}

	revoked, renewedCookie, err := s.auth.RevokeSessions(body.All, curCookie)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	if body.All {
		clearSessionCookie(w, r)
	} else if renewedCookie != "" {
		setSessionCookie(w, r, renewedCookie)
	}
	writeJSON(w, http.StatusOK, map[string]any{"revoked": revoked})
}

func initAuthManagerFallback(dataDir string) *auth.Manager {
	dir := strings.TrimSpace(dataDir)
	if dir == "" {
		dir, _ = os.MkdirTemp("", "luma-eye-auth-*")
	}
	mgr, err := auth.NewManager(dir)
	if err != nil {
		tmp, _ := os.MkdirTemp("", "luma-eye-auth-fallback-*")
		mgr, _ = auth.NewManager(tmp)
	}
	return mgr
}
