package scrub

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestScrubMapAndURL(t *testing.T) {
	in := map[string]any{
		"username":      "alice",
		"password":      "super-secret-123",
		"authorization": "Bearer xyz",
		"nested": map[string]any{
			"api_key": "k_live_999",
			"url":     "https://demo.example.com/cb?token=secret_tok&page=2",
		},
	}
	out := ScrubMap(in)
	if out["username"] != "alice" {
		t.Fatalf("username altered: %v", out["username"])
	}
	if out["password"] != "[REDACTED]" || out["authorization"] != "[REDACTED]" {
		t.Fatalf("sensitive fields not redacted: %+v", out)
	}
	nested := out["nested"].(map[string]any)
	if nested["api_key"] != "[REDACTED]" {
		t.Fatalf("nested api_key not redacted: %+v", nested)
	}
	u := nested["url"].(string)
	if u != "https://demo.example.com/cb?page=2&token=%5BREDACTED%5D" && u != "https://demo.example.com/cb?token=%5BREDACTED%5D&page=2" {
		t.Fatalf("unexpected scrubbed url: %s", u)
	}
}

func TestExtractClientIPAndUA(t *testing.T) {
	// Trusted reverse proxy (loopback/private/CGNAT) -> honor proxy headers
	req := httptest.NewRequest(http.MethodPost, "/_e/b", nil)
	req.RemoteAddr = "127.0.0.1:43210"
	req.Header.Set("Ali-Real-Client-IP", "203.0.113.9")
	req.Header.Set("X-Forwarded-For", "10.0.0.1, 10.0.0.2")
	req.Header.Set("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36")

	if got := ExtractClientIP(req); got != "203.0.113.9" {
		t.Fatalf("expected Ali-Real-Client-IP from trusted proxy, got %q", got)
	}

	// Untrusted public peer -> ignore spoofed proxy headers and return RemoteAddr host
	pubReq := httptest.NewRequest(http.MethodPost, "/_e/b", nil)
	pubReq.RemoteAddr = "198.51.100.42:51234"
	pubReq.Header.Set("Ali-Real-Client-IP", "127.0.0.1")
	pubReq.Header.Set("CF-Connecting-IP", "203.0.113.9")
	pubReq.Header.Set("X-Real-IP", "203.0.113.9")
	pubReq.Header.Set("X-Forwarded-For", "10.0.0.1")
	if got := ExtractClientIP(pubReq); got != "198.51.100.42" {
		t.Fatalf("expected RemoteAddr host for public peer, got %q", got)
	}

	// Verify RFC1918, IPv6 loopback, and CGNAT/Tailscale proxy peers are trusted
	for _, proxyAddr := range []string{
		"127.0.0.1:8080",
		"[::1]:8080",
		"10.0.0.5:1234",
		"172.18.0.2:1234",
		"192.168.1.10:1234",
		"100.64.0.9:1234",
		"100.127.255.254:1234",
		"[fd7a:115c:a1e0::1]:1234",
	} {
		pr := httptest.NewRequest(http.MethodPost, "/_e/b", nil)
		pr.RemoteAddr = proxyAddr
		pr.Header.Set("X-Forwarded-For", "203.0.113.55, 10.0.0.1")
		if got := ExtractClientIP(pr); got != "203.0.113.55" {
			t.Fatalf("proxyAddr=%s: expected forwarded IP 203.0.113.55, got %q", proxyAddr, got)
		}
	}

	// Non-CGNAT 100.x public IP must NOT be treated as a trusted proxy
	nonCGNAT := httptest.NewRequest(http.MethodPost, "/_e/b", nil)
	nonCGNAT.RemoteAddr = "100.128.0.1:4444"
	nonCGNAT.Header.Set("X-Real-IP", "203.0.113.99")
	if got := ExtractClientIP(nonCGNAT); got != "100.128.0.1" {
		t.Fatalf("expected public 100.128.0.1 to ignore X-Real-IP, got %q", got)
	}

	ua := ParseUserAgent(req.Header.Get("User-Agent"))
	if ua.IsBot || ua.Browser != "Chrome" || ua.OS != "macOS" || ua.DeviceType != "desktop" {
		t.Fatalf("unexpected ua parse: %+v", ua)
	}

	bot := ParseUserAgent("Mozilla/5.0 (compatible; luma-route-check/1.0)")
	if !bot.IsBot {
		t.Fatalf("expected bot detection for luma-route-check")
	}

	androidRN := ParseUserAgent("okhttp/4.12.0")
	if androidRN.OS != "Android" || androidRN.DeviceType != "mobile" || androidRN.Browser != "React Native" {
		t.Fatalf("unexpected Android RN ua parse: %+v", androidRN)
	}

	iosRN := ParseUserAgent("MyApp/1 CFNetwork/1496.0.7 Darwin/23.5.0")
	if iosRN.OS != "iOS" || iosRN.DeviceType != "mobile" || iosRN.Browser != "React Native" {
		t.Fatalf("unexpected iOS RN ua parse: %+v", iosRN)
	}
}
