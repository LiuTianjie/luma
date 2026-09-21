package deployment

import (
	"testing"

	"github.com/LiuTianjie/luma/internal/storage"
)

func TestPrerequisitesDistinguishExplicitCLIAndVerifiedDashboard(t *testing.T) {
	s := Service{Name: "app", Image: "private.test/app:1", Region: "home", Exposure: "none", Proxy: true, Environment: map[string]string{"TOKEN": "${APP}"}}
	snapshot := Snapshot{Nodes: []storage.Node{{Name: "worker", Region: "home", Ready: true}}, NodeAddresses: map[string]NodeAddress{"worker": {Tailnet: true}}}
	state := PrerequisiteState{Secrets: map[string]bool{"EGRESS_SUBSCRIPTION_URL": true}, Registries: map[string]bool{"private.test": true}, Verified: map[string]bool{}}
	cli := requirementsFor(s, snapshot, state, DNSConfig{}, map[string]bool{"APP": true}, false)
	if !cli.Ready || cli.Require("app", true) != nil {
		t.Fatal(cli)
	}
	dashboard := requirementsFor(s, snapshot, state, DNSConfig{}, map[string]bool{"APP": true}, true)
	if dashboard.Ready || dashboard.Require("app", false) == nil || dashboard.Require("app", true) != nil {
		t.Fatal(dashboard)
	}
	s.Requirements.Capabilities = []string{"egress"}
	dashboard = requirementsFor(s, snapshot, state, DNSConfig{}, map[string]bool{"APP": true}, true)
	if dashboard.Require("app", true) == nil {
		t.Fatal("explicit prerequisite was not enforced")
	}
	state.Verified["egress"] = true
	dashboard = requirementsFor(s, snapshot, state, DNSConfig{}, map[string]bool{"APP": true}, true)
	if !dashboard.Ready || dashboard.Require("app", false) != nil {
		t.Fatal(dashboard)
	}
}

func TestInitUsesProviderPrerequisitesAndCloudflareConfiguredTokenName(t *testing.T) {
	s := Service{Image: "busybox:1.36", Exposure: "cloudflare-tunnel", Region: "cn", TunnelTokenEnv: "TUNNEL", Requirements: Requirements{Init: []string{"cloudflare-tunnel"}}}
	state := PrerequisiteState{Secrets: map[string]bool{"PRIVATE_CF_TOKEN": true}}
	r := requirementsFor(s, Snapshot{}, state, DNSConfig{Provider: "cloudflare", TokenEnv: "PRIVATE_CF_TOKEN"}, map[string]bool{"TUNNEL": true}, false)
	if err := r.Require("app", true); err != nil {
		t.Fatal(err)
	}
	state.Secrets["PRIVATE_CF_TOKEN"] = false
	r = requirementsFor(s, Snapshot{}, state, DNSConfig{Provider: "cloudflare", TokenEnv: "PRIVATE_CF_TOKEN"}, map[string]bool{"TUNNEL": true}, false)
	if err := r.Require("app", true); err == nil {
		t.Fatal("init accepted a missing provider token")
	}
}
