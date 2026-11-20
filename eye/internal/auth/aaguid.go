package auth

import (
	"fmt"
	"strings"
)

const ZeroAAGUID = "00000000-0000-0000-0000-000000000000"

// Well-known FIDO/Passkey provider AAGUIDs from community passkey-authenticator-aaguids registry.
// Used strictly as UX hints for passkey management labels, never for security decisions.
var knownAAGUIDProviders = map[string]string{
	"fbfc3007-154e-4ecc-8c0b-6e020557d7bd": "iCloud Keychain",
	"dd4ec289-e01d-41c9-bb89-70fa845d4bf2": "iCloud Keychain (Managed)",
	"00000000-0000-0000-0000-000000000001": "Platform Authenticator",
	"ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4": "Google Password Manager",
	"adce0002-35bc-c60a-648b-0b25f1f05503": "Chrome on Mac",
	"bada5566-a7aa-401f-bd96-45619a55120d": "1Password",
	"d548826e-79b4-db40-a3d8-11116f7e8349": "Bitwarden",
	"b5397666-4885-aa6b-cebf-e52262a439a2": "Dashlane",
	"53414d53-554e-4700-0000-000000000000": "Samsung Pass",
	"08987058-cadc-4b81-b6e1-30de50dcbe96": "Windows Hello",
	"9ddd1817-af5a-4672-a2b9-3e3dd95000a9": "Windows Hello",
	"6028b017-b1d4-4c02-b4b3-afcdafc96bb2": "Windows Hello",
	"2fc0579f-8113-47ea-b116-bb5a8db9202a": "YubiKey 5 Series",
	"ee882879-721c-4913-9775-3dfcce97072a": "YubiKey Bio",
	"cb69481e-8ff7-4039-93ec-0a2729a154a8": "YubiKey 5 Series",
	"f8a011f3-8c0a-4d15-8006-17111f9edc7d": "YubiKey Security Key",
	"0ea242b4-43c4-4a1b-8b17-dd6d0b6baec6": "Proton Pass",
}

func FormatAAGUID(raw []byte) string {
	if len(raw) != 16 {
		return ZeroAAGUID
	}
	return strings.ToLower(fmt.Sprintf(
		"%08x-%04x-%04x-%04x-%012x",
		raw[0:4], raw[4:6], raw[6:8], raw[8:10], raw[10:16],
	))
}

func LookupProviderName(aaguid string) string {
	clean := strings.ToLower(strings.TrimSpace(aaguid))
	if clean == "" || clean == ZeroAAGUID {
		return ""
	}
	return knownAAGUIDProviders[clean]
}

func ResolveDefaultPasskeyName(aaguid, userAgent string) string {
	if strings.ToLower(strings.TrimSpace(aaguid)) != ZeroAAGUID {
		if provider := LookupProviderName(aaguid); provider != "" {
			return provider
		}
	}
	if uaLabel := DescribeUserAgent(userAgent); uaLabel != "" && uaLabel != "-" {
		return uaLabel
	}
	return "Passkey"
}

func DescribeUserAgent(userAgent string) string {
	ua := strings.TrimSpace(userAgent)
	if ua == "" {
		return "-"
	}
	browser := ""
	switch {
	case strings.Contains(ua, "Edg/"):
		browser = "Edge"
	case strings.Contains(ua, "Firefox/"):
		browser = "Firefox"
	case strings.Contains(ua, "Chrome/"):
		browser = "Chrome"
	case strings.Contains(ua, "Safari/"):
		browser = "Safari"
	}
	osName := ""
	switch {
	case strings.Contains(ua, "iPhone") || strings.Contains(ua, "iPad"):
		osName = "iOS"
	case strings.Contains(ua, "Android"):
		osName = "Android"
	case strings.Contains(ua, "Mac OS X") || strings.Contains(ua, "Macintosh"):
		osName = "macOS"
	case strings.Contains(ua, "Windows"):
		osName = "Windows"
	case strings.Contains(ua, "Linux"):
		osName = "Linux"
	}
	if browser != "" && osName != "" {
		return browser + " · " + osName
	}
	if osName != "" {
		return osName
	}
	if browser != "" {
		return browser
	}
	if len(ua) > 48 {
		return ua[:48]
	}
	return ua
}
