package auth

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"
)

var (
	controlProofRe   = regexp.MustCompile(`^[A-Za-z0-9_-]{43}$`)
	controlSessionRe = regexp.MustCompile(`^las_[A-Za-z0-9_-]{43}$`)
	controlAddonIDRe = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}$`)

	ErrControlUnavailable = errors.New("luma control authentication is unavailable")
	ErrControlUnauthorized = errors.New("luma control rejected credentials")
)

// ControlSession represents an active browser session verified against Luma Control.
type ControlSession struct {
	Authenticated bool   `json:"authenticated"`
	Token         string `json:"token,omitempty"`
	ExpiresAt     int64  `json:"expiresAt"`
	Audience      string `json:"audience"`
	AddonID       string `json:"addonId,omitempty"`
	Actor         string `json:"actor,omitempty"`
}

// ControlClient bridges Luma Eye to Luma Control's unified IdP endpoints:
//   - Operator/CLI Bearer tokens: POST /v1/auth/login/verify (short SHA-256 digest cache)
//   - Browser SSO: PKCE handoff via /dashboard/addon-login + POST /v1/auth/addon/{id}/{exchange,verify,logout}
//
// Browser sessions are NEVER cached so parent logout and passkey revocation on
// Control take effect immediately.
type ControlClient struct {
	endpoint string
	addonID  string
	ttl      time.Duration
	client   *http.Client

	mu    sync.Mutex
	cache map[[32]byte]time.Time
}

func NewControlClient(endpoint, addonID string, insecure bool, cacheTTL time.Duration) (*ControlClient, error) {
	clean := strings.TrimRight(strings.TrimSpace(endpoint), "/")
	if clean == "" {
		return nil, nil
	}
	u, err := url.Parse(clean)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" {
		return nil, fmt.Errorf("invalid LUMA_CONTROL_URL: %q", endpoint)
	}
	cleanAddon := strings.ToLower(strings.TrimSpace(addonID))
	if cleanAddon == "" {
		cleanAddon = "eye"
	}
	if !controlAddonIDRe.MatchString(cleanAddon) {
		return nil, fmt.Errorf("invalid LUMA_ADDON_ID: %q", addonID)
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if insecure {
		transport.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} //nolint:gosec // Explicit operator opt-in for dev Control
	}
	httpClient := &http.Client{
		Timeout:   8 * time.Second,
		Transport: transport,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	if cacheTTL < 0 {
		cacheTTL = 0
	}
	return &ControlClient{
		endpoint: clean,
		addonID:  cleanAddon,
		ttl:      cacheTTL,
		client:   httpClient,
		cache:    make(map[[32]byte]time.Time),
	}, nil
}

func (c *ControlClient) Endpoint() string {
	if c == nil {
		return ""
	}
	return c.endpoint
}

func (c *ControlClient) AddonID() string {
	if c == nil {
		return ""
	}
	return c.addonID
}

func (c *ControlClient) SecuritySettingsURL() string {
	if c == nil || c.endpoint == "" {
		return ""
	}
	return c.endpoint + "/dashboard/settings/security"
}

// StartLogin creates a fresh PKCE verifier/challenge pair and returns the Control redirect URL.
func (c *ControlClient) StartLogin(theme string) (redirectURL string, verifier string, err error) {
	if c == nil {
		return "", "", ErrControlUnavailable
	}
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", "", err
	}
	verifier = base64.RawURLEncoding.EncodeToString(raw)
	sum := sha256.Sum256([]byte(verifier))
	challenge := base64.RawURLEncoding.EncodeToString(sum[:])

	q := url.Values{}
	q.Set("addon", c.addonID)
	q.Set("challenge", challenge)
	cleanTheme := strings.ToLower(strings.TrimSpace(theme))
	if cleanTheme == "light" || cleanTheme == "dark" {
		q.Set("theme", cleanTheme)
	}
	return c.endpoint + "/dashboard/addon-login?" + q.Encode(), verifier, nil
}

// VerifyToken checks an operator Luma deploy token via Control's /v1/auth/login/verify.
// Only SHA-256 digests are cached for c.ttl; plaintext tokens are never stored.
func (c *ControlClient) VerifyToken(ctx context.Context, token string) (bool, error) {
	if c == nil {
		return false, nil
	}
	clean := strings.TrimSpace(token)
	if clean == "" {
		return false, nil
	}
	digest := sha256.Sum256([]byte(clean))
	now := time.Now()
	if c.ttl > 0 {
		c.mu.Lock()
		exp, ok := c.cache[digest]
		if ok && now.Before(exp) {
			c.mu.Unlock()
			return true, nil
		}
		if ok {
			delete(c.cache, digest)
		}
		c.mu.Unlock()
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint+"/v1/auth/login/verify", bytes.NewReader([]byte("{}")))
	if err != nil {
		return false, err
	}
	req.Header.Set("Authorization", "Bearer "+clean)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	resp, err := c.client.Do(req)
	if err != nil {
		return false, ErrControlUnavailable
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		return false, nil
	}
	if resp.StatusCode != http.StatusOK {
		return false, ErrControlUnavailable
	}
	var payload struct {
		ClusterID string `json:"clusterId"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 16<<10)).Decode(&payload); err != nil {
		return false, ErrControlUnavailable
	}
	if strings.TrimSpace(payload.ClusterID) == "" {
		return false, nil
	}
	if c.ttl > 0 {
		c.mu.Lock()
		if len(c.cache) >= 64 {
			for k, exp := range c.cache {
				if now.After(exp) {
					delete(c.cache, k)
				}
			}
			if len(c.cache) >= 64 {
				clear(c.cache)
			}
		}
		c.cache[digest] = now.Add(c.ttl)
		c.mu.Unlock()
	}
	return true, nil
}

// ExchangeCode swaps a one-use PKCE ticket + verifier for an opaque Control addon session.
func (c *ControlClient) ExchangeCode(ctx context.Context, code, verifier string) (*ControlSession, error) {
	if c == nil {
		return nil, ErrControlUnavailable
	}
	if !controlProofRe.MatchString(code) || !controlProofRe.MatchString(verifier) {
		return nil, ErrControlUnauthorized
	}
	var sess ControlSession
	if err := c.postAddonAuth(ctx, "exchange", map[string]string{
		"code":     code,
		"verifier": verifier,
	}, "", &sess); err != nil {
		return nil, err
	}
	if !sess.Authenticated || !controlSessionRe.MatchString(sess.Token) || sess.ExpiresAt <= time.Now().Unix() || !isValidHTTPSAudience(sess.Audience) {
		return nil, ErrControlUnauthorized
	}
	return &sess, nil
}

// VerifySession validates an opaque Control addon session uncached on every request.
func (c *ControlClient) VerifySession(ctx context.Context, sessionToken string) (*ControlSession, error) {
	if c == nil {
		return nil, ErrControlUnavailable
	}
	if !controlSessionRe.MatchString(sessionToken) {
		return nil, ErrControlUnauthorized
	}
	var sess ControlSession
	if err := c.postAddonAuth(ctx, "verify", nil, sessionToken, &sess); err != nil {
		return nil, err
	}
	if !sess.Authenticated || !isValidHTTPSAudience(sess.Audience) {
		return nil, ErrControlUnauthorized
	}
	return &sess, nil
}

// LogoutSession terminates the addon session on Control (leaving parent session active).
func (c *ControlClient) LogoutSession(ctx context.Context, sessionToken string) error {
	if c == nil || !controlSessionRe.MatchString(sessionToken) {
		return nil
	}
	var resp map[string]any
	return c.postAddonAuth(ctx, "logout", nil, sessionToken, &resp)
}

func (c *ControlClient) postAddonAuth(ctx context.Context, action string, body any, bearer string, out any) error {
	if body == nil {
		body = map[string]any{}
	}
	encoded, err := json.Marshal(body)
	if err != nil {
		return err
	}
	endpoint := fmt.Sprintf("%s/v1/auth/addon/%s/%s", c.endpoint, c.addonID, action)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(encoded))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if bearer != "" {
		req.Header.Set("Authorization", "Bearer "+bearer)
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return ErrControlUnavailable
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		return ErrControlUnauthorized
	}
	if resp.StatusCode != http.StatusOK {
		return ErrControlUnavailable
	}
	if out != nil {
		if err := json.NewDecoder(io.LimitReader(resp.Body, 16<<10)).Decode(out); err != nil {
			return ErrControlUnavailable
		}
	}
	return nil
}

func isValidHTTPSAudience(raw string) bool {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Scheme != "https" || u.Host == "" || u.Path != "" || u.RawQuery != "" || u.Fragment != "" {
		return false
	}
	return true
}

func IsValidControlSessionToken(token string) bool {
	return controlSessionRe.MatchString(token)
}
