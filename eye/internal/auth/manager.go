package auth

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/go-webauthn/webauthn/webauthn"
	"golang.org/x/crypto/argon2"
)

const (
	SessionTTL            = 30 * 24 * time.Hour
	sessionRenewThreshold = 20 * 24 * time.Hour
	maxStoredSessions     = 64
	lastSeenPersistWindow = 10 * time.Minute
)

var (
	ErrPasskeyNotFound = errors.New("passkey credential not found")
	ErrSessionInvalid  = errors.New("invalid or expired session")
)

type StoredPasskey struct {
	ID                   string            `json:"id"`
	Credential           webauthn.Credential `json:"credential"`
	Name                 string            `json:"name"`
	AAGUID               string            `json:"aaguid"`
	ProviderName         string            `json:"provider_name,omitempty"`
	Transports           []string          `json:"transports,omitempty"`
	CredentialDeviceType string            `json:"credential_device_type"`
	CredentialBackedUp   bool              `json:"credential_backed_up"`
	SignCount            uint32            `json:"sign_count"`
	RegisteredAt         int64             `json:"registered_at"`
	LastUsedAt           int64             `json:"last_used_at,omitempty"`
}

type PasskeyView struct {
	ID                   string   `json:"id"`
	Name                 string   `json:"name"`
	AAGUID               string   `json:"aaguid"`
	ProviderName         string   `json:"providerName,omitempty"`
	Transports           []string `json:"transports"`
	CredentialDeviceType string   `json:"credentialDeviceType"`
	CredentialBackedUp   bool     `json:"credentialBackedUp"`
	Counter              uint32   `json:"counter"`
	RegisteredAt         int64    `json:"registeredAt"`
	LastUsedAt           int64    `json:"lastUsedAt,omitempty"`
}

func (p StoredPasskey) View() PasskeyView {
	transports := p.Transports
	if transports == nil {
		transports = []string{}
	}
	devType := p.CredentialDeviceType
	if devType == "" {
		if p.Credential.Flags.BackupEligible {
			devType = "multiDevice"
		} else {
			devType = "singleDevice"
		}
	}
	return PasskeyView{
		ID:                   p.ID,
		Name:                 p.Name,
		AAGUID:               p.AAGUID,
		ProviderName:         p.ProviderName,
		Transports:           transports,
		CredentialDeviceType: devType,
		CredentialBackedUp:   p.CredentialBackedUp,
		Counter:              p.SignCount,
		RegisteredAt:         p.RegisteredAt,
		LastUsedAt:           p.LastUsedAt,
	}
}

type BrowserSessionRecord struct {
	ID          string `json:"id"`
	Method      string `json:"method"` // "token" | "password" | "passkey"
	PasskeyID   string `json:"passkey_id,omitempty"`
	PasskeyName string `json:"passkey_name,omitempty"`
	UserAgent   string `json:"user_agent,omitempty"`
	CreatedAt   int64  `json:"created_at"`
	LastSeenAt  int64  `json:"last_seen_at"`
	ExpiresAt   int64  `json:"expires_at"`
}

type BrowserSessionView struct {
	ID          string `json:"id"`
	Method      string `json:"method"`
	PasskeyName string `json:"passkeyName,omitempty"`
	UserAgent   string `json:"userAgent"`
	CreatedAt   int64  `json:"createdAt"`
	LastSeenAt  int64  `json:"lastSeenAt"`
	ExpiresAt   int64  `json:"expiresAt"`
	Current     bool   `json:"current"`
}

type AccountSnapshot struct {
	UserID       string `json:"userId"`
	Username     string `json:"username"`
	DisplayName  string `json:"displayName"`
	HasPassword  bool   `json:"hasPassword"`
	PasskeyCount int    `json:"passkeyCount"`
}

type persistedState struct {
	SessionSecret string                 `json:"session_secret"`
	SessionEpoch  int                    `json:"session_epoch"`
	UserID        string                 `json:"user_id"`
	Username      string                 `json:"username"`
	DisplayName   string                 `json:"display_name"`
	PasswordSalt  string                 `json:"password_salt,omitempty"`
	PasswordHash  string                 `json:"password_hash,omitempty"`
	Passkeys      []StoredPasskey        `json:"passkeys,omitempty"`
	Sessions      []BrowserSessionRecord `json:"sessions,omitempty"`
}

type Manager struct {
	mu          sync.RWMutex
	filePath    string
	state       persistedState
	secretBytes []byte
	lastFlushAt time.Time

	ceremonyMu sync.Mutex
	ceremonies map[string]*pendingCeremony
}

func NewManager(dataDir string) (*Manager, error) {
	metaDir := filepath.Join(dataDir, "meta")
	if err := os.MkdirAll(metaDir, 0o755); err != nil {
		return nil, fmt.Errorf("create auth meta dir: %w", err)
	}
	fp := filepath.Join(metaDir, "auth_state.json")
	m := &Manager{
		filePath:   fp,
		ceremonies: make(map[string]*pendingCeremony),
	}
	if err := m.loadOrInit(); err != nil {
		return nil, err
	}
	return m, nil
}

func (m *Manager) loadOrInit() error {
	m.mu.Lock()
	defer m.mu.Unlock()

	dirty := false
	if raw, err := os.ReadFile(m.filePath); err == nil && len(raw) > 0 {
		if err := json.Unmarshal(raw, &m.state); err != nil {
			return fmt.Errorf("decode %s: %w", m.filePath, err)
		}
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("read %s: %w", m.filePath, err)
	} else {
		dirty = true
	}

	if len(m.state.SessionSecret) < 64 {
		m.state.SessionSecret = randomHexBytes(32)
		dirty = true
	}
	sec, err := hex.DecodeString(m.state.SessionSecret)
	if err != nil || len(sec) < 32 {
		m.state.SessionSecret = randomHexBytes(32)
		sec, _ = hex.DecodeString(m.state.SessionSecret)
		dirty = true
	}
	m.secretBytes = sec

	if m.state.SessionEpoch < 1 {
		m.state.SessionEpoch = 1
		dirty = true
	}
	if !IsValidUnpaddedBase64URL(m.state.UserID) {
		uidBytes := make([]byte, 16)
		_, _ = rand.Read(uidBytes)
		m.state.UserID = base64.RawURLEncoding.EncodeToString(uidBytes)
		dirty = true
	}
	if strings.TrimSpace(m.state.Username) == "" {
		m.state.Username = "admin"
		dirty = true
	}
	if strings.TrimSpace(m.state.DisplayName) == "" {
		m.state.DisplayName = "Administrator"
		dirty = true
	}

	nowMs := time.Now().UnixMilli()
	pruned := m.state.Sessions[:0]
	for _, s := range m.state.Sessions {
		if s.ExpiresAt > nowMs {
			pruned = append(pruned, s)
		} else {
			dirty = true
		}
	}
	m.state.Sessions = pruned

	if dirty {
		return m.saveLocked()
	}
	return nil
}

func (m *Manager) saveLocked() error {
	raw, err := json.MarshalIndent(&m.state, "", "  ")
	if err != nil {
		return err
	}
	raw = append(raw, '\n')
	tmp := m.filePath + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, m.filePath); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	m.lastFlushAt = time.Now()
	return nil
}

func (m *Manager) Snapshot() AccountSnapshot {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return AccountSnapshot{
		UserID:       m.state.UserID,
		Username:     m.state.Username,
		DisplayName:  m.state.DisplayName,
		HasPassword:  m.state.PasswordHash != "" && m.state.PasswordSalt != "",
		PasskeyCount: len(m.state.Passkeys),
	}
}

func (m *Manager) HasPassword() bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.state.PasswordHash != "" && m.state.PasswordSalt != ""
}

func (m *Manager) VerifyPassword(username, password string) bool {
	if password == "" {
		return false
	}
	m.mu.RLock()
	expectedUser := m.state.Username
	saltB64 := m.state.PasswordSalt
	hashB64 := m.state.PasswordHash
	m.mu.RUnlock()

	if saltB64 == "" || hashB64 == "" {
		return false
	}
	cleanUser := strings.TrimSpace(username)
	if cleanUser != "" && !strings.EqualFold(cleanUser, expectedUser) {
		return false
	}
	salt, err1 := base64.RawURLEncoding.DecodeString(saltB64)
	expectedHash, err2 := base64.RawURLEncoding.DecodeString(hashB64)
	if err1 != nil || err2 != nil || len(salt) < 8 || len(expectedHash) == 0 {
		return false
	}
	computed := argon2.IDKey([]byte(password), salt, 3, 64*1024, 4, uint32(len(expectedHash)))
	return subtle.ConstantTimeCompare(computed, expectedHash) == 1
}

func (m *Manager) SetPassword(newPassword string) error {
	runes := utf8.RuneCountInString(newPassword)
	if runes < 8 {
		return fmt.Errorf("密码长度至少需要 8 个字符")
	}
	if len(newPassword) > 256 {
		return fmt.Errorf("密码长度不能超过 256 字节")
	}
	salt := make([]byte, 16)
	if _, err := rand.Read(salt); err != nil {
		return err
	}
	hash := argon2.IDKey([]byte(newPassword), salt, 3, 64*1024, 4, 32)

	m.mu.Lock()
	defer m.mu.Unlock()
	m.state.PasswordSalt = base64.RawURLEncoding.EncodeToString(salt)
	m.state.PasswordHash = base64.RawURLEncoding.EncodeToString(hash)
	return m.saveLocked()
}

func (m *Manager) UpdateProfile(name, displayName string) (AccountSnapshot, error) {
	cleanName := strings.TrimSpace(name)
	cleanDisplay := strings.TrimSpace(displayName)
	if cleanName == "" {
		return AccountSnapshot{}, fmt.Errorf("用户名不能为空")
	}
	if utf8.RuneCountInString(cleanName) > 64 {
		return AccountSnapshot{}, fmt.Errorf("用户名长度不能超过 64 个字符")
	}
	if cleanDisplay == "" {
		cleanDisplay = cleanName
	}
	if utf8.RuneCountInString(cleanDisplay) > 80 {
		return AccountSnapshot{}, fmt.Errorf("显示名称长度不能超过 80 个字符")
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	m.state.Username = cleanName
	m.state.DisplayName = cleanDisplay
	if err := m.saveLocked(); err != nil {
		return AccountSnapshot{}, err
	}
	return AccountSnapshot{
		UserID:       m.state.UserID,
		Username:     m.state.Username,
		DisplayName:  m.state.DisplayName,
		HasPassword:  m.state.PasswordHash != "" && m.state.PasswordSalt != "",
		PasskeyCount: len(m.state.Passkeys),
	}, nil
}

// CreateSession creates a persistent 30-day browser session stored on disk and returns
// its signed cookie token + record. Survives container upgrades & restarts.
func (m *Manager) CreateSession(method, passkeyID, passkeyName, userAgent string) (string, BrowserSessionRecord, error) {
	now := time.Now()
	exp := now.Add(SessionTTL)
	sidBytes := make([]byte, 16)
	if _, err := rand.Read(sidBytes); err != nil {
		return "", BrowserSessionRecord{}, err
	}
	sid := base64.RawURLEncoding.EncodeToString(sidBytes)

	rec := BrowserSessionRecord{
		ID:          sid,
		Method:      method,
		PasskeyID:   passkeyID,
		PasskeyName: passkeyName,
		UserAgent:   truncateString(strings.TrimSpace(userAgent), 240),
		CreatedAt:   now.UnixMilli(),
		LastSeenAt:  now.UnixMilli(),
		ExpiresAt:   exp.UnixMilli(),
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	nowMs := now.UnixMilli()
	kept := make([]BrowserSessionRecord, 0, len(m.state.Sessions)+1)
	for _, s := range m.state.Sessions {
		if s.ExpiresAt > nowMs {
			kept = append(kept, s)
		}
	}
	kept = append(kept, rec)
	if len(kept) > maxStoredSessions {
		sort.Slice(kept, func(i, j int) bool {
			return kept[i].LastSeenAt < kept[j].LastSeenAt
		})
		kept = kept[len(kept)-maxStoredSessions:]
	}
	m.state.Sessions = kept
	if err := m.saveLocked(); err != nil {
		return "", BrowserSessionRecord{}, err
	}

	tok := m.signTokenLocked(m.state.SessionEpoch, sid, exp.Unix())
	return tok, rec, nil
}

// VerifySession validates the signed cookie token against the persistent session store.
// If valid and less than 20 days remain, it returns a renewed 30-day cookie token.
func (m *Manager) VerifySession(cookieValue string) (rec BrowserSessionRecord, renewedCookie string, ok bool) {
	epoch, sid, expUnix, validSig := m.parseAndVerifySignature(cookieValue)
	if !validSig {
		return BrowserSessionRecord{}, "", false
	}
	now := time.Now()
	if now.Unix() >= expUnix {
		return BrowserSessionRecord{}, "", false
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	if epoch != m.state.SessionEpoch {
		return BrowserSessionRecord{}, "", false
	}

	idx := -1
	for i := range m.state.Sessions {
		if m.state.Sessions[i].ID == sid {
			idx = i
			break
		}
	}
	if idx < 0 {
		return BrowserSessionRecord{}, "", false
	}
	found := &m.state.Sessions[idx]
	if now.UnixMilli() >= found.ExpiresAt {
		return BrowserSessionRecord{}, "", false
	}

	found.LastSeenAt = now.UnixMilli()
	remaining := time.Until(time.UnixMilli(found.ExpiresAt))
	shouldPersist := time.Since(m.lastFlushAt) >= lastSeenPersistWindow
	if remaining < sessionRenewThreshold {
		newExp := now.Add(SessionTTL)
		found.ExpiresAt = newExp.UnixMilli()
		renewedCookie = m.signTokenLocked(m.state.SessionEpoch, found.ID, newExp.Unix())
		shouldPersist = true
	}
	out := *found
	if shouldPersist {
		_ = m.saveLocked()
	}
	return out, renewedCookie, true
}

func (m *Manager) ListSessions(currentCookie string) []BrowserSessionView {
	_, currentSID, _, _ := m.parseAndVerifySignature(currentCookie)
	nowMs := time.Now().UnixMilli()

	m.mu.RLock()
	defer m.mu.RUnlock()

	out := make([]BrowserSessionView, 0, len(m.state.Sessions))
	for _, s := range m.state.Sessions {
		if s.ExpiresAt <= nowMs {
			continue
		}
		out = append(out, BrowserSessionView{
			ID:          s.ID,
			Method:      s.Method,
			PasskeyName: s.PasskeyName,
			UserAgent:   s.UserAgent,
			CreatedAt:   s.CreatedAt,
			LastSeenAt:  s.LastSeenAt,
			ExpiresAt:   s.ExpiresAt,
			Current:     currentSID != "" && s.ID == currentSID,
		})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Current != out[j].Current {
			return out[i].Current
		}
		return out[i].LastSeenAt > out[j].LastSeenAt
	})
	return out
}

func (m *Manager) RevokeSessionByCookie(cookieValue string) {
	_, sid, _, ok := m.parseAndVerifySignature(cookieValue)
	if !ok || sid == "" {
		return
	}
	_, _ = m.RevokeSession(sid)
}

func (m *Manager) RevokeSession(sessionID string) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	removed := 0
	kept := m.state.Sessions[:0]
	for _, s := range m.state.Sessions {
		if s.ID == sessionID {
			removed++
			continue
		}
		kept = append(kept, s)
	}
	m.state.Sessions = kept
	if removed > 0 {
		if err := m.saveLocked(); err != nil {
			return removed, err
		}
	}
	return removed, nil
}

// RevokeOtherOrAllSessions revokes either all sessions or all sessions except exceptCookie.
func (m *Manager) RevokeSessions(all bool, exceptCookie string) (int, string, error) {
	_, keepSID, _, _ := m.parseAndVerifySignature(exceptCookie)

	m.mu.Lock()
	defer m.mu.Unlock()

	now := time.Now()
	nowMs := now.UnixMilli()
	if all || keepSID == "" {
		count := len(m.state.Sessions)
		m.state.Sessions = nil
		m.state.SessionEpoch++
		if err := m.saveLocked(); err != nil {
			return 0, "", err
		}
		return count, "", nil
	}

	var keptRecord *BrowserSessionRecord
	revoked := 0
	for i := range m.state.Sessions {
		s := m.state.Sessions[i]
		if s.ExpiresAt <= nowMs {
			continue
		}
		if s.ID == keepSID {
			cp := s
			cp.ExpiresAt = now.Add(SessionTTL).UnixMilli()
			cp.LastSeenAt = nowMs
			keptRecord = &cp
		} else {
			revoked++
		}
	}
	m.state.SessionEpoch++
	var newToken string
	if keptRecord != nil {
		m.state.Sessions = []BrowserSessionRecord{*keptRecord}
		newToken = m.signTokenLocked(m.state.SessionEpoch, keptRecord.ID, now.Add(SessionTTL).Unix())
	} else {
		m.state.Sessions = nil
	}
	if err := m.saveLocked(); err != nil {
		return 0, "", err
	}
	return revoked, newToken, nil
}

func (m *Manager) ListPasskeys() (userID string, list []PasskeyView) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	list = make([]PasskeyView, 0, len(m.state.Passkeys))
	for _, pk := range m.state.Passkeys {
		list = append(list, pk.View())
	}
	sort.Slice(list, func(i, j int) bool {
		return list[i].RegisteredAt > list[j].RegisteredAt
	})
	return m.state.UserID, list
}

func (m *Manager) RenamePasskey(credID, newName string) (PasskeyView, error) {
	clean := strings.TrimSpace(newName)
	if clean == "" {
		return PasskeyView{}, fmt.Errorf("Passkey 名称不能为空")
	}
	if utf8.RuneCountInString(clean) > 64 {
		return PasskeyView{}, fmt.Errorf("Passkey 名称长度不能超过 64 个字符")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	for i := range m.state.Passkeys {
		if m.state.Passkeys[i].ID == credID {
			m.state.Passkeys[i].Name = clean
			if err := m.saveLocked(); err != nil {
				return PasskeyView{}, err
			}
			return m.state.Passkeys[i].View(), nil
		}
	}
	return PasskeyView{}, ErrPasskeyNotFound
}

func (m *Manager) DeletePasskey(credID string) (signedOut int, userID string, err error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	found := false
	keptPK := m.state.Passkeys[:0]
	for _, pk := range m.state.Passkeys {
		if pk.ID == credID {
			found = true
			continue
		}
		keptPK = append(keptPK, pk)
	}
	if !found {
		return 0, m.state.UserID, ErrPasskeyNotFound
	}
	m.state.Passkeys = keptPK

	keptSess := m.state.Sessions[:0]
	for _, s := range m.state.Sessions {
		if s.PasskeyID == credID {
			signedOut++
			continue
		}
		keptSess = append(keptSess, s)
	}
	m.state.Sessions = keptSess
	if err := m.saveLocked(); err != nil {
		return 0, m.state.UserID, err
	}
	return signedOut, m.state.UserID, nil
}

func (m *Manager) signTokenLocked(epoch int, sid string, expUnix int64) string {
	payload := fmt.Sprintf("v1.%d.%s.%d", epoch, sid, expUnix)
	mac := hmac.New(sha256.New, m.secretBytes)
	_, _ = mac.Write([]byte(payload))
	sig := hex.EncodeToString(mac.Sum(nil))
	return payload + "." + sig
}

func (m *Manager) parseAndVerifySignature(raw string) (epoch int, sid string, expUnix int64, ok bool) {
	parts := strings.Split(strings.TrimSpace(raw), ".")
	if len(parts) != 5 || parts[0] != "v1" {
		return 0, "", 0, false
	}
	ep, err1 := strconv.Atoi(parts[1])
	exp, err2 := strconv.ParseInt(parts[3], 10, 64)
	sigBytes, err3 := hex.DecodeString(parts[4])
	if err1 != nil || err2 != nil || err3 != nil || parts[2] == "" {
		return 0, "", 0, false
	}

	m.mu.RLock()
	secret := m.secretBytes
	m.mu.RUnlock()
	if len(secret) == 0 {
		return 0, "", 0, false
	}

	payload := strings.Join(parts[:4], ".")
	mac := hmac.New(sha256.New, secret)
	_, _ = mac.Write([]byte(payload))
	expected := mac.Sum(nil)
	if subtle.ConstantTimeCompare(sigBytes, expected) != 1 {
		return 0, "", 0, false
	}
	return ep, parts[2], exp, true
}

func IsValidUnpaddedBase64URL(s string) bool {
	if len(s) == 0 || len(s)%4 == 1 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		isAlphaNum := (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_'
		if !isAlphaNum {
			return false
		}
	}
	return true
}

func randomHexBytes(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func truncateString(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max]
}
