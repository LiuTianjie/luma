package deployment

import (
	"reflect"
	"testing"
)

func TestEgressGatewayResolutionAndEnvironment(t *testing.T) {
	base := EgressSettings{NomadRPCAddr: "https://100.64.0.1:4647", NomadServerAddr: "100.64.0.2", ManagerHosts: []string{"100.64.0.3"}, NomadHTTPAddr: "http://100.64.0.4:4646", Regions: map[string]string{"private": "proxy", "direct": "direct"}}
	c := RenderConfig{NomadServer: "100.64.0.5", NomadAddr: "http://100.64.0.6:4646"}
	for _, name := range []string{"cn", "home", "private"} {
		got, err := ResolveEgressProxy(name, base, c)
		if err != nil || got != "http://100.64.0.1:7890" {
			t.Fatal(name, got, err)
		}
	}
	for _, name := range []string{"global", "direct"} {
		got, err := ResolveEgressProxy(name, base, c)
		if err != nil || got != "" {
			t.Fatal(name, got, err)
		}
	}
	base.NomadRPCAddr = ""
	got, _ := ResolveEgressProxy("cn", base, c)
	if got != "http://100.64.0.2:7890" {
		t.Fatal(got)
	}
	base.NomadServerAddr = ""
	got, _ = ResolveEgressProxy("cn", base, c)
	if got != "http://100.64.0.5:7890" {
		t.Fatal(got)
	}
	c.NomadServer = ""
	got, _ = ResolveEgressProxy("cn", base, c)
	if got != "http://100.64.0.3:7890" {
		t.Fatal(got)
	}
	base.ManagerHosts = nil
	got, _ = ResolveEgressProxy("cn", base, c)
	if got != "http://100.64.0.4:7890" {
		t.Fatal(got)
	}
	base.NomadHTTPAddr = ""
	got, _ = ResolveEgressProxy("cn", base, c)
	if got != "http://100.64.0.6:7890" {
		t.Fatal(got)
	}
	for _, value := range []string{"[fd00::1]:4647", "fd00::1", "http://[fd00::1]:4647"} {
		got, err := ResolveEgressProxy("cn", EgressSettings{NomadRPCAddr: value}, RenderConfig{})
		if err != nil || got != "http://[fd00::1]:7890" {
			t.Fatal(value, got, err)
		}
	}
	for _, value := range []string{"", "localhost:4646", "http://127.0.0.1:4646", "http://[::1]:4646", "http://user:private@host:4646", "http://host/path"} {
		if _, err := ResolveEgressProxy("cn", EgressSettings{NomadHTTPAddr: value}, RenderConfig{}); err == nil {
			t.Fatal("accepted unavailable/invalid gateway", value)
		}
	}
	s := Service{Proxy: true, Environment: map[string]string{"HTTP_PROXY": "${USER_PROXY}", "NO_PROXY": "local", "KEEP": "one"}}
	if !needsEgressDefaults(s) {
		t.Fatal("missing HTTPS default was ignored")
	}
	env := RuntimeEnvironment(s, "http://manager:7890")
	if env["HTTP_PROXY"] != "${USER_PROXY}" || env["HTTPS_PROXY"] != "http://manager:7890" || env["NO_PROXY"] != "local" {
		t.Fatal(env)
	}
	if _, ok := s.Environment["HTTPS_PROXY"]; ok {
		t.Fatal("modified original service")
	}
	s.Proxy = false
	if needsEgressDefaults(s) {
		t.Fatal("proxy without opt-in")
	}
	if !reflect.DeepEqual(RuntimeEnvironment(s, "http://manager:7890"), s.Environment) {
		t.Fatal("injected proxy without opt-in")
	}
	s = Service{Proxy: true, Environment: map[string]string{"HTTP_PROXY": "", "HTTPS_PROXY": "http://custom"}}
	if needsEgressDefaults(s) {
		t.Fatal("explicit proxy configuration requires no gateway default")
	}
}
