package scrub

import (
	"crypto/sha256"
	"encoding/hex"
	"net"
	"net/http"
	"net/url"
	"strings"
)

var sensitiveKeys = []string{
	"password",
	"passwd",
	"pwd",
	"secret",
	"token",
	"access_token",
	"refresh_token",
	"id_token",
	"authorization",
	"cookie",
	"set-cookie",
	"api_key",
	"apikey",
	"access_key",
	"secret_key",
	"private_key",
	"credit_card",
	"card_number",
	"cvv",
	"ssn",
}

func IsSensitiveKey(key string) bool {
	low := strings.ToLower(strings.TrimSpace(key))
	if low == "" {
		return false
	}
	for _, s := range sensitiveKeys {
		if low == s || strings.Contains(low, s) {
			return true
		}
	}
	return false
}

func ScrubMap(m map[string]any) map[string]any {
	if m == nil {
		return nil
	}
	out := make(map[string]any, len(m))
	for k, v := range m {
		if IsSensitiveKey(k) {
			out[k] = "[REDACTED]"
			continue
		}
		out[k] = ScrubValue(v)
	}
	return out
}

func ScrubValue(v any) any {
	switch tv := v.(type) {
	case map[string]any:
		return ScrubMap(tv)
	case []any:
		out := make([]any, len(tv))
		for i, elem := range tv {
			out[i] = ScrubValue(elem)
		}
		return out
	case string:
		if strings.Contains(tv, "?") && (strings.HasPrefix(tv, "http://") || strings.HasPrefix(tv, "https://") || strings.HasPrefix(tv, "/")) {
			return ScrubURL(tv)
		}
		if len(tv) > 4096 {
			return tv[:4096]
		}
		return tv
	default:
		return v
	}
}

func ScrubStringMap(m map[string]string) map[string]string {
	if m == nil {
		return nil
	}
	out := make(map[string]string, len(m))
	for k, v := range m {
		if IsSensitiveKey(k) {
			out[k] = "[REDACTED]"
		} else {
			out[k] = v
		}
	}
	return out
}

func ScrubURL(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	u, err := url.Parse(raw)
	if err != nil {
		return raw
	}
	if u.User != nil {
		u.User = url.User("[REDACTED]")
	}
	if u.RawQuery != "" {
		q := u.Query()
		changed := false
		for k := range q {
			if IsSensitiveKey(k) {
				q.Set(k, "[REDACTED]")
				changed = true
			}
		}
		if changed {
			u.RawQuery = q.Encode()
		}
	}
	return u.String()
}

func ExtractPath(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "/"
	}
	if strings.HasPrefix(raw, "/") && !strings.HasPrefix(raw, "//") {
		if idx := strings.IndexByte(raw, '?'); idx >= 0 {
			return raw[:idx]
		}
		return raw
	}
	u, err := url.Parse(raw)
	if err != nil || u.Path == "" {
		return "/"
	}
	return u.Path
}

func ExtractClientIP(r *http.Request) string {
	if r == nil {
		return ""
	}
	for _, hdr := range []string{"Ali-Real-Client-IP", "CF-Connecting-IP", "X-Real-IP"} {
		if v := strings.TrimSpace(r.Header.Get(hdr)); v != "" {
			return stripPort(v)
		}
	}
	if xff := strings.TrimSpace(r.Header.Get("X-Forwarded-For")); xff != "" {
		first, _, _ := strings.Cut(xff, ",")
		if first = strings.TrimSpace(first); first != "" {
			return stripPort(first)
		}
	}
	return stripPort(r.RemoteAddr)
}

func ExtractCountry(r *http.Request) string {
	if r == nil {
		return ""
	}
	for _, hdr := range []string{"CF-IPCountry", "Ali-Ip-Country", "X-Country-Code"} {
		if v := strings.ToUpper(strings.TrimSpace(r.Header.Get(hdr))); v != "" && v != "XX" {
			return v
		}
	}
	return ""
}

func HashIP(ip, salt string) string {
	if ip == "" {
		return ""
	}
	sum := sha256.Sum256([]byte(salt + ":" + ip))
	return hex.EncodeToString(sum[:8])
}

func stripPort(addr string) string {
	addr = strings.TrimSpace(addr)
	if host, _, err := net.SplitHostPort(addr); err == nil {
		return host
	}
	return addr
}

type UAInfo struct {
	Browser    string
	OS         string
	DeviceType string
	IsBot      bool
}

var botTokens = []string{
	"bot", "spider", "crawler", "slurp", "headlesschrome", "phantomjs",
	"lighthouse", "pagespeed", "uptime", "pingdom", "luma-route-check",
	"bingpreview", "facebookexternalhit", "bytespider", "gptbot", "claudebot",
}

func ParseUserAgent(ua string) UAInfo {
	low := strings.ToLower(ua)
	info := UAInfo{
		Browser:    "Other",
		OS:         "Other",
		DeviceType: "desktop",
	}
	if low == "" {
		return info
	}

	for _, tok := range botTokens {
		if strings.Contains(low, tok) {
			info.IsBot = true
			info.Browser = "Bot"
			info.DeviceType = "bot"
			return info
		}
	}

	// OS detection
	switch {
	case strings.Contains(low, "iphone") || strings.Contains(low, "ipad") || strings.Contains(low, "ios"):
		info.OS = "iOS"
		if strings.Contains(low, "ipad") {
			info.DeviceType = "tablet"
		} else {
			info.DeviceType = "mobile"
		}
	case strings.Contains(low, "android"):
		info.OS = "Android"
		if strings.Contains(low, "mobile") {
			info.DeviceType = "mobile"
		} else {
			info.DeviceType = "tablet"
		}
	case strings.Contains(low, "harmonyos"):
		info.OS = "HarmonyOS"
		info.DeviceType = "mobile"
	case strings.Contains(low, "mac os x") || strings.Contains(low, "macintosh"):
		info.OS = "macOS"
	case strings.Contains(low, "windows"):
		info.OS = "Windows"
	case strings.Contains(low, "linux"):
		info.OS = "Linux"
	case strings.Contains(low, "okhttp/"):
		info.OS = "Android"
		info.DeviceType = "mobile"
	case strings.Contains(low, "cfnetwork/") && strings.Contains(low, "darwin/"):
		info.OS = "iOS"
		info.DeviceType = "mobile"
	}

	// Browser detection
	switch {
	case strings.Contains(low, "micromessenger"):
		info.Browser = "WeChat"
	case strings.Contains(low, "edg/") || strings.Contains(low, "edge/"):
		info.Browser = "Edge"
	case strings.Contains(low, "firefox/") || strings.Contains(low, "fxios/"):
		info.Browser = "Firefox"
	case strings.Contains(low, "chrome/") || strings.Contains(low, "crios/"):
		info.Browser = "Chrome"
	case strings.Contains(low, "safari/"):
		info.Browser = "Safari"
	case strings.Contains(low, "expo"):
		info.Browser = "Expo"
	case strings.Contains(low, "okhttp/") || (strings.Contains(low, "cfnetwork/") && strings.Contains(low, "darwin/")):
		info.Browser = "React Native"
	case strings.Contains(low, "curl/") || strings.Contains(low, "python-urllib") || strings.Contains(low, "go-http-client"):
		info.Browser = "CLI/SDK"
		info.DeviceType = "server"
	}

	return info
}

func NormalizeReferrer(ref, pageURL string) string {
	ref = strings.TrimSpace(ref)
	if ref == "" {
		return ""
	}
	ru, err := url.Parse(ref)
	if err != nil || ru.Host == "" {
		return ""
	}
	if pageURL != "" {
		if pu, err := url.Parse(pageURL); err == nil && strings.EqualFold(ru.Host, pu.Host) {
			return ""
		}
	}
	return strings.ToLower(ru.Hostname())
}
