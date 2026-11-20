package geoip

import "testing"

func TestEmbeddedCountryDatabase(t *testing.T) {
	for ip, expected := range map[string]string{"8.8.8.8": "US", "2001:4860:4860::8888": "CA"} {
		if got := Country(ip); got != expected {
			t.Errorf("Country(%s) = %q", ip, got)
		}
	}
	for _, ip := range []string{"", "invalid", "127.0.0.1", "10.0.0.1", "::1", "fd00::1"} {
		if got := Country(ip); got != "" {
			t.Errorf("private/invalid address resolved: %q", got)
		}
	}
}
