package region

import "testing"

func TestEgressRegistrationAndBuiltins(t *testing.T) {
	custom := map[string]string{"cn": "direct", "global": "proxy", "private": "", "abroad": "direct", "bad": "invalid"}
	for name, expected := range map[string]string{"cn": "proxy", "home": "proxy", "global": "direct", "private": "proxy", "abroad": "direct"} {
		got, err := EgressMode(name, custom)
		if err != nil || got != expected {
			t.Fatal(name, got, err)
		}
	}
	for _, name := range []string{"unregistered", "bad", "../bad"} {
		if _, err := EgressMode(name, custom); err == nil {
			t.Fatal("accepted", name)
		}
	}
}
