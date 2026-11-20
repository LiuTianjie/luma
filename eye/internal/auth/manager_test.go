package auth

import (
	"testing"
)

func TestManagerPersistenceAcrossRestarts(t *testing.T) {
	dir := t.TempDir()

	m1, err := NewManager(dir)
	if err != nil {
		t.Fatalf("NewManager 1: %v", err)
	}
	snap1 := m1.Snapshot()
	if !IsValidUnpaddedBase64URL(snap1.UserID) {
		t.Fatalf("expected valid unpadded Base64URL UserID, got %q", snap1.UserID)
	}
	if snap1.Username != "admin" {
		t.Fatalf("expected default username admin, got %q", snap1.Username)
	}

	if err := m1.SetPassword("MyStrongPass#2026"); err != nil {
		t.Fatalf("SetPassword: %v", err)
	}
	cookieTok, _, err := m1.CreateSession("password", "", "", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15")
	if err != nil {
		t.Fatalf("CreateSession: %v", err)
	}

	// Simulate container upgrade/restart: brand new Manager reading same persistent DataDir
	m2, err := NewManager(dir)
	if err != nil {
		t.Fatalf("NewManager 2: %v", err)
	}
	rec, _, ok := m2.VerifySession(cookieTok)
	if !ok {
		t.Fatalf("expected session cookie to remain valid across restart")
	}
	if rec.Method != "password" {
		t.Fatalf("expected method password, got %q", rec.Method)
	}
	if !m2.VerifyPassword("admin", "MyStrongPass#2026") {
		t.Fatalf("expected custom password to verify after restart")
	}
	if m2.VerifyPassword("admin", "wrong-password") {
		t.Fatalf("expected wrong password to fail")
	}

	// Create a second session and revoke others while keeping the first
	cookie2, _, err := m2.CreateSession("token", "", "", "Chrome/134.0 Windows NT 10.0")
	if err != nil {
		t.Fatalf("CreateSession 2: %v", err)
	}
	revoked, renewed1, err := m2.RevokeSessions(false, cookieTok)
	if err != nil {
		t.Fatalf("RevokeSessions(others): %v", err)
	}
	if revoked != 1 || renewed1 == "" {
		t.Fatalf("expected 1 revoked and renewed token for current browser, got revoked=%d renewed=%q", revoked, renewed1)
	}
	if _, _, ok := m2.VerifySession(cookie2); ok {
		t.Fatalf("expected second session to be revoked")
	}
	if _, _, ok := m2.VerifySession(renewed1); !ok {
		t.Fatalf("expected renewed current session to remain valid")
	}
}

func TestBeginWebAuthnOptionsShape(t *testing.T) {
	dir := t.TempDir()
	m, err := NewManager(dir)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	regOpts, cid1, err := m.BeginRegistration("https://eye.itool.tech", "eye.itool.tech", "MacBook Touch ID", false)
	if err != nil {
		t.Fatalf("BeginRegistration: %v", err)
	}
	if cid1 == "" {
		t.Fatalf("expected non-empty ceremony id")
	}
	rpMap, _ := regOpts["rp"].(map[string]any)
	if rpMap["id"] != "eye.itool.tech" {
		t.Fatalf("expected rp.id=eye.itool.tech, got %v", rpMap["id"])
	}
	userMap, _ := regOpts["user"].(map[string]any)
	uidStr, _ := userMap["id"].(string)
	if !IsValidUnpaddedBase64URL(uidStr) || uidStr != m.Snapshot().UserID {
		t.Fatalf("expected user.id=%q, got %q", m.Snapshot().UserID, uidStr)
	}
	if _, hasAttachment := regOpts["authenticatorSelection"].(map[string]any)["authenticatorAttachment"]; hasAttachment {
		t.Fatalf("expected management flow to omit authenticatorAttachment")
	}

	promoOpts, _, err := m.BeginRegistration("https://eye.itool.tech", "eye.itool.tech", "", true)
	if err != nil {
		t.Fatalf("BeginRegistration(promo): %v", err)
	}
	if promoOpts["authenticatorSelection"].(map[string]any)["authenticatorAttachment"] != "platform" {
		t.Fatalf("expected promotion flow to set authenticatorAttachment=platform")
	}

	loginOpts, cid2, err := m.BeginLogin("https://eye.itool.tech", "eye.itool.tech")
	if err != nil {
		t.Fatalf("BeginLogin: %v", err)
	}
	if cid2 == "" || loginOpts["rpId"] != "eye.itool.tech" || loginOpts["userVerification"] != "preferred" {
		t.Fatalf("unexpected loginOpts: %#v", loginOpts)
	}
	allowList, ok := loginOpts["allowCredentials"].([]any)
	if !ok || len(allowList) != 0 {
		t.Fatalf("expected empty allowCredentials slice, got %#v", loginOpts["allowCredentials"])
	}
}

func TestZeroAAGUIDSkipsProviderLookup(t *testing.T) {
	if got := LookupProviderName(ZeroAAGUID); got != "" {
		t.Fatalf("expected empty provider for zero AAGUID, got %q", got)
	}
	label := ResolveDefaultPasskeyName(ZeroAAGUID, "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Version/18.0 Safari/605.1.15")
	if label != "Safari · macOS" {
		t.Fatalf("expected UA fallback Safari · macOS, got %q", label)
	}
	icLabel := ResolveDefaultPasskeyName("fbfc3007-154e-4ecc-8c0b-6e020557d7bd", "")
	if icLabel != "iCloud Keychain" {
		t.Fatalf("expected iCloud Keychain, got %q", icLabel)
	}
}
