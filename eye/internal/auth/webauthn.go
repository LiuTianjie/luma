package auth

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"strings"
	"time"

	"github.com/go-webauthn/webauthn/protocol"
	"github.com/go-webauthn/webauthn/webauthn"
)

const (
	ceremonyTTL       = 5 * time.Minute
	maxOpenCeremonies = 64
)

type ceremonyKind string

const (
	ceremonyRegister ceremonyKind = "register"
	ceremonyLogin    ceremonyKind = "login"
)

type pendingCeremony struct {
	id         string
	kind       ceremonyKind
	origin     string
	rpID       string
	customName string
	session    webauthn.SessionData
	expiresAt  time.Time
}

type waUser struct {
	id          []byte
	name        string
	displayName string
	credentials []webauthn.Credential
}

func (u *waUser) WebAuthnID() []byte                         { return u.id }
func (u *waUser) WebAuthnName() string                       { return u.name }
func (u *waUser) WebAuthnDisplayName() string                { return u.displayName }
func (u *waUser) WebAuthnCredentials() []webauthn.Credential { return u.credentials }

func ResolveRPOriginAndID(publicURL string) (origin string, rpID string, err error) {
	u, err := url.Parse(strings.TrimSpace(publicURL))
	if err != nil || u.Scheme == "" || u.Host == "" {
		return "", "", fmt.Errorf("invalid public URL %q for WebAuthn", publicURL)
	}
	host := u.Host
	hostname := u.Hostname()
	if ip := net.ParseIP(hostname); ip != nil {
		if ip.IsLoopback() {
			// Browsers forbid raw IP addresses as WebAuthn RP ID; map loopback IP to localhost in dev.
			hostname = "localhost"
		} else {
			return "", "", fmt.Errorf("Passkey 需要绑定域名访问（当前为裸 IP %s）", hostname)
		}
	}
	return u.Scheme + "://" + host, strings.ToLower(hostname), nil
}

func newWebAuthn(origin, rpID string) (*webauthn.WebAuthn, error) {
	origins := []string{origin}
	// In local dev, allow both localhost and 127.0.0.1 port matching if localhost RP ID is used.
	if rpID == "localhost" && strings.HasPrefix(origin, "http://127.0.0.1") {
		origins = append(origins, strings.Replace(origin, "http://127.0.0.1", "http://localhost", 1))
	}
	return webauthn.New(&webauthn.Config{
		RPID:                              rpID,
		RPDisplayName:                     "Luma Eye",
		RPOrigins:                         origins,
		AttestationPreference:             protocol.PreferNoAttestation,
		ExtensionsUnsolicitedOutputPolicy: protocol.UnsolicitedOutputPolicyIgnore,
	})
}

func (m *Manager) buildWAUser() (*waUser, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()

	uidBytes, err := base64.RawURLEncoding.DecodeString(m.state.UserID)
	if err != nil || len(uidBytes) == 0 {
		return nil, fmt.Errorf("invalid stored user_id")
	}
	creds := make([]webauthn.Credential, 0, len(m.state.Passkeys))
	for _, pk := range m.state.Passkeys {
		creds = append(creds, pk.Credential)
	}
	return &waUser{
		id:          uidBytes,
		name:        m.state.Username,
		displayName: m.state.DisplayName,
		credentials: creds,
	}, nil
}

func (m *Manager) storeCeremony(c *pendingCeremony) string {
	id := randomHexBytes(16)
	c.id = id
	c.expiresAt = time.Now().Add(ceremonyTTL)

	m.ceremonyMu.Lock()
	defer m.ceremonyMu.Unlock()

	now := time.Now()
	for k, v := range m.ceremonies {
		if now.After(v.expiresAt) {
			delete(m.ceremonies, k)
		}
	}
	if len(m.ceremonies) >= maxOpenCeremonies {
		var oldestKey string
		var oldestExp time.Time
		for k, v := range m.ceremonies {
			if oldestKey == "" || v.expiresAt.Before(oldestExp) {
				oldestKey = k
				oldestExp = v.expiresAt
			}
		}
		if oldestKey != "" {
			delete(m.ceremonies, oldestKey)
		}
	}
	m.ceremonies[id] = c
	return id
}

func (m *Manager) consumeCeremony(kind ceremonyKind, ceremonyID string, responseChallenge string) (*pendingCeremony, error) {
	m.ceremonyMu.Lock()
	defer m.ceremonyMu.Unlock()

	now := time.Now()
	for k, v := range m.ceremonies {
		if now.After(v.expiresAt) {
			delete(m.ceremonies, k)
		}
	}

	if ceremonyID != "" {
		if c, ok := m.ceremonies[ceremonyID]; ok && c.kind == kind {
			if responseChallenge == "" || c.session.Challenge == responseChallenge {
				delete(m.ceremonies, ceremonyID)
				return c, nil
			}
		}
	}

	// Fallback by matching server-issued challenge (handles multi-tab Conditional UI + explicit button races cleanly).
	if responseChallenge != "" {
		for k, c := range m.ceremonies {
			if c.kind == kind && c.session.Challenge == responseChallenge {
				delete(m.ceremonies, k)
				return c, nil
			}
		}
	}

	return nil, fmt.Errorf("WebAuthn 验证会话已过期或无效，请重试")
}

// BeginRegistration generates WebAuthn PublicKeyCredentialCreationOptionsJSON for native
// PublicKeyCredential.parseCreationOptionsFromJSON() consumption.
func (m *Manager) BeginRegistration(origin, rpID, customName string, promotion bool) (map[string]any, string, error) {
	wa, err := newWebAuthn(origin, rpID)
	if err != nil {
		return nil, "", err
	}
	user, err := m.buildWAUser()
	if err != nil {
		return nil, "", err
	}

	exclusions := make([]protocol.CredentialDescriptor, 0, len(user.credentials))
	for _, cred := range user.credentials {
		exclusions = append(exclusions, cred.Descriptor())
	}

	reqResKey := true
	selection := protocol.AuthenticatorSelection{
		ResidentKey:        protocol.ResidentKeyRequirementRequired,
		RequireResidentKey: &reqResKey,
		UserVerification:   protocol.VerificationPreferred,
	}
	if promotion {
		selection.AuthenticatorAttachment = protocol.Platform
	}

	creation, session, err := wa.BeginRegistration(
		user,
		webauthn.WithAuthenticatorSelection(selection),
		webauthn.WithExclusions(exclusions),
		webauthn.WithConveyancePreference(protocol.PreferNoAttestation),
	)
	if err != nil {
		return nil, "", err
	}

	rawJSON, err := json.Marshal(creation.Response)
	if err != nil {
		return nil, "", err
	}
	var opts map[string]any
	if err := json.Unmarshal(rawJSON, &opts); err != nil {
		return nil, "", err
	}

	// Ensure user.id is strictly unpadded Base64URL string matching m.Snapshot().UserID.
	if userMap, ok := opts["user"].(map[string]any); ok {
		userMap["id"] = base64.RawURLEncoding.EncodeToString(user.id)
	}
	// Ensure excludeCredentials always serializes as an array with unpadded Base64URL ids.
	exclOut := make([]map[string]any, 0, len(exclusions))
	for _, ex := range exclusions {
		item := map[string]any{
			"type": "public-key",
			"id":   base64.RawURLEncoding.EncodeToString(ex.CredentialID),
		}
		if len(ex.Transport) > 0 {
			ts := make([]string, 0, len(ex.Transport))
			for _, t := range ex.Transport {
				ts = append(ts, string(t))
			}
			item["transports"] = ts
		}
		exclOut = append(exclOut, item)
	}
	opts["excludeCredentials"] = exclOut

	ceremonyID := m.storeCeremony(&pendingCeremony{
		kind:       ceremonyRegister,
		origin:     origin,
		rpID:       rpID,
		customName: strings.TrimSpace(customName),
		session:    *session,
	})
	return opts, ceremonyID, nil
}

func (m *Manager) FinishRegistration(ceremonyID string, bodyBytes []byte, userAgent string) (PasskeyView, string, string, error) {
	parsed, err := protocol.ParseCredentialCreationResponseBytes(bodyBytes)
	if err != nil {
		return PasskeyView{}, "", "", fmt.Errorf("invalid attestation payload: %w", err)
	}
	ceremony, err := m.consumeCeremony(ceremonyRegister, ceremonyID, parsed.Response.CollectedClientData.Challenge)
	if err != nil {
		return PasskeyView{}, "", "", err
	}

	wa, err := newWebAuthn(ceremony.origin, ceremony.rpID)
	if err != nil {
		return PasskeyView{}, "", "", err
	}
	user, err := m.buildWAUser()
	if err != nil {
		return PasskeyView{}, "", "", err
	}

	cred, err := wa.CreateCredential(user, ceremony.session, parsed)
	if err != nil {
		return PasskeyView{}, "", "", fmt.Errorf("verify passkey attestation: %w", err)
	}

	credID := base64.RawURLEncoding.EncodeToString(cred.ID)
	aaguid := FormatAAGUID(cred.Authenticator.AAGUID)
	providerName := LookupProviderName(aaguid)
	name := strings.TrimSpace(ceremony.customName)
	if name == "" {
		name = ResolveDefaultPasskeyName(aaguid, userAgent)
	}

	var transports []string
	seenT := make(map[string]bool)
	for _, t := range cred.Transport {
		s := strings.TrimSpace(string(t))
		if s != "" && !seenT[s] {
			seenT[s] = true
			transports = append(transports, s)
		}
	}
	for _, t := range parsed.Response.Transports {
		s := strings.TrimSpace(string(t))
		if s != "" && !seenT[s] {
			seenT[s] = true
			transports = append(transports, s)
		}
	}

	devType := "singleDevice"
	if cred.Flags.BackupEligible {
		devType = "multiDevice"
	}

	stored := StoredPasskey{
		ID:                   credID,
		Credential:           *cred,
		Name:                 truncateString(name, 64),
		AAGUID:               aaguid,
		ProviderName:         providerName,
		Transports:           transports,
		CredentialDeviceType: devType,
		CredentialBackedUp:   cred.Flags.BackupState,
		SignCount:            cred.Authenticator.SignCount,
		RegisteredAt:         time.Now().UnixMilli(),
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	for _, existing := range m.state.Passkeys {
		if existing.ID == credID {
			return PasskeyView{}, ceremony.rpID, m.state.UserID, fmt.Errorf("该 Passkey 已注册")
		}
	}
	m.state.Passkeys = append(m.state.Passkeys, stored)
	if err := m.saveLocked(); err != nil {
		return PasskeyView{}, ceremony.rpID, m.state.UserID, err
	}
	return stored.View(), ceremony.rpID, m.state.UserID, nil
}

// BeginLogin generates discoverable WebAuthn PublicKeyCredentialRequestOptionsJSON
// for native PublicKeyCredential.parseRequestOptionsFromJSON() consumption.
func (m *Manager) BeginLogin(origin, rpID string) (map[string]any, string, error) {
	wa, err := newWebAuthn(origin, rpID)
	if err != nil {
		return nil, "", err
	}

	assertion, session, err := wa.BeginDiscoverableLogin(
		webauthn.WithUserVerification(protocol.VerificationPreferred),
	)
	if err != nil {
		return nil, "", err
	}
	// Persist expected UV level inside server session record per WebAuthn guidance.
	session.UserVerification = protocol.VerificationPreferred

	rawJSON, err := json.Marshal(assertion.Response)
	if err != nil {
		return nil, "", err
	}
	var opts map[string]any
	if err := json.Unmarshal(rawJSON, &opts); err != nil {
		return nil, "", err
	}
	opts["rpId"] = rpID
	opts["allowCredentials"] = []any{}
	opts["userVerification"] = string(protocol.VerificationPreferred)

	ceremonyID := m.storeCeremony(&pendingCeremony{
		kind:    ceremonyLogin,
		origin:  origin,
		rpID:    rpID,
		session: *session,
	})
	return opts, ceremonyID, nil
}

// FinishLogin verifies a discoverable passkey assertion. Returns ErrPasskeyNotFound
// when the credential ID does not exist in storage so HTTP 404 can trigger Signal API cleanup.
func (m *Manager) FinishLogin(ceremonyID string, bodyBytes []byte) (PasskeyView, error) {
	parsed, err := protocol.ParseCredentialRequestResponseBytes(bytes.TrimSpace(bodyBytes))
	if err != nil {
		return PasskeyView{}, fmt.Errorf("invalid assertion payload: %w", err)
	}
	credID := base64.RawURLEncoding.EncodeToString(parsed.RawID)

	m.mu.RLock()
	exists := false
	for _, pk := range m.state.Passkeys {
		if pk.ID == credID {
			exists = true
			break
		}
	}
	m.mu.RUnlock()
	if !exists {
		// Consume ceremony if present, then return explicit 404 sentinel for signalUnknownCredential.
		_, _ = m.consumeCeremony(ceremonyLogin, ceremonyID, parsed.Response.CollectedClientData.Challenge)
		return PasskeyView{}, ErrPasskeyNotFound
	}

	ceremony, err := m.consumeCeremony(ceremonyLogin, ceremonyID, parsed.Response.CollectedClientData.Challenge)
	if err != nil {
		return PasskeyView{}, err
	}

	wa, err := newWebAuthn(ceremony.origin, ceremony.rpID)
	if err != nil {
		return PasskeyView{}, err
	}
	user, err := m.buildWAUser()
	if err != nil {
		return PasskeyView{}, err
	}

	handler := func(rawID, userHandle []byte) (webauthn.User, error) {
		if len(userHandle) > 0 && !bytes.Equal(userHandle, user.id) {
			return nil, ErrPasskeyNotFound
		}
		for _, c := range user.credentials {
			if bytes.Equal(c.ID, rawID) {
				return user, nil
			}
		}
		return nil, ErrPasskeyNotFound
	}

	_, validatedCred, err := wa.ValidatePasskeyLogin(handler, ceremony.session, parsed)
	if err != nil {
		if errors.Is(err, ErrPasskeyNotFound) {
			return PasskeyView{}, ErrPasskeyNotFound
		}
		return PasskeyView{}, fmt.Errorf("passkey signature verification failed: %w", err)
	}

	nowMs := time.Now().UnixMilli()
	m.mu.Lock()
	defer m.mu.Unlock()
	for i := range m.state.Passkeys {
		if m.state.Passkeys[i].ID == credID {
			m.state.Passkeys[i].Credential = *validatedCred
			m.state.Passkeys[i].SignCount = validatedCred.Authenticator.SignCount
			m.state.Passkeys[i].LastUsedAt = nowMs
			m.state.Passkeys[i].CredentialBackedUp = validatedCred.Flags.BackupState
			_ = m.saveLocked()
			return m.state.Passkeys[i].View(), nil
		}
	}
	return PasskeyView{}, ErrPasskeyNotFound
}
