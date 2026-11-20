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
			"url":     "https://vibecheck.itool.tech/cb?token=secret_tok&page=2",
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
	if u != "https://vibecheck.itool.tech/cb?page=2&token=%5BREDACTED%5D" && u != "https://vibecheck.itool.tech/cb?token=%5BREDACTED%5D&page=2" {
		t.Fatalf("unexpected scrubbed url: %s", u)
	}
}

func TestExtractClientIPAndUA(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/_e/b", nil)
	req.Header.Set("Ali-Real-Client-IP", "203.0.113.9")
	req.Header.Set("X-Forwarded-For", "10.0.0.1, 10.0.0.2")
	req.Header.Set("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36")

	if got := ExtractClientIP(req); got != "203.0.113.9" {
		t.Fatalf("expected Ali-Real-Client-IP, got %q", got)
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
