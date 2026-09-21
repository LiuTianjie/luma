package deployment

import "testing"

func TestDNSDefaultsOverridesAndRegionalTarget(t *testing.T) {
	nodes := map[string]NodeAddress{"cn-edge": {Region: "cn", Roles: []string{"edge"}, PublicIP: "192.0.2.1"}, "global-edge": {Region: "global", Roles: []string{"edge"}, PublicIP: "192.0.2.2"}}
	config := DNSConfig{Provider: "cloudflare", RecordType: "A", Proxied: true, TTL: 1}
	services := []Service{{Name: "a", Region: "cn", Exposure: "cn-edge", Domain: "a.example.test"}, {Name: "b", Region: "global", Exposure: "external-edge", Domain: "b.example.test"}}
	got, err := PlanDNS(services, config, nodes, false)
	if err != nil || len(got) != 2 || got[0].Content != "192.0.2.1" || got[1].Content != "192.0.2.2" {
		t.Fatal(got, err)
	}
	disabled, ttl := false, 300
	services[0].DNS = DNSOptions{Target: "origin.example.test", Type: "cname", Proxied: &disabled, TTL: &ttl}
	got, err = PlanDNS(services, config, nodes, false)
	if err != nil || got[0].Type != "CNAME" || got[0].TTL != 300 || got[0].Proxied {
		t.Fatal(got, err)
	}
	got, err = PlanDNS(services, config, nil, true)
	if err != nil || len(got) != 0 {
		t.Fatal(got, err)
	}
}
