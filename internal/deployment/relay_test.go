package deployment

import (
	"reflect"
	"testing"
)

func TestRelayPortOwnershipAndRefresh(t *testing.T) {
	reservations := []RelayReservation{{Kind: "service", Slug: "db", Status: "active", Ports: []int{5432}}, {Kind: "compose", Slug: "pending", Status: "pending", Ports: []int{9000}}, {Kind: "service", Slug: "gone", Status: "removed", Ports: []int{9001}}}
	for _, ports := range [][]int{{5432}, {9000}, {8000, 8000}, {80}, {443}} {
		if err := CheckRelayPorts("service", "new", ports, reservations); err == nil {
			t.Fatal("accepted conflict", ports)
		}
	}
	if err := CheckRelayPorts("service", "db", []int{5432, 9001}, reservations); err != nil {
		t.Fatal(err)
	}
	if got := RelayRefreshPorts([]int{5432, 9000, 9001}, reservations); !reflect.DeepEqual(got, []int{9000, 9001}) {
		t.Fatal(got)
	}
}
func TestRelayTargetsUseCanonicalNodeAndPreserveExplicitUpstreams(t *testing.T) {
	nodes := map[string]NodeAddress{"worker": {Address: "100.64.0.7"}}
	for _, exposure := range []string{"tailscale-relay", "tcp-relay"} {
		s := Service{Node: "worker", Exposure: exposure, Port: 80, PublishPort: 8080}
		got, err := ResolveRelay(s, nodes)
		if err != nil {
			t.Fatal(err)
		}
		if exposure == "tailscale-relay" && (got.Relay.Host != "100.64.0.7" || got.Relay.Port != 8080) || exposure == "tcp-relay" && (got.TCP.Host != "100.64.0.7" || got.TCP.Port != 8080) {
			t.Fatal(got)
		}
		s.Node = ""
		if _, err := ResolveRelay(s, nodes); err == nil {
			t.Fatal("accepted unpinned unresolved relay")
		}
		s.Relay.URLs = []string{"http://origin.test:81"}
		s.TCP.Addresses = []string{"origin.test:82"}
		got, err = ResolveRelay(s, nodes)
		if err != nil || !reflect.DeepEqual(got, s) {
			t.Fatal(got, err)
		}
	}
}
