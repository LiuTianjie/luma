package storage

import (
	"errors"
	"testing"
)

func TestOwnershipFollowsExistingDataNode(t *testing.T) {
	mount := Mount{Type: "volume", Source: "db-data", Target: "/data"}
	nodes := []Node{{Name: "a-new", ID: "new-id", Region: "cn", Ready: true}, {Name: "original", ID: "old-id", Region: "cn", Ready: true, Aliases: []string{"old-name"}}}
	base := OwnershipRequest{Region: "cn", Current: []Mount{mount}, Nodes: nodes, Runtime: PlacementEvidence{Exists: true, OwnerIDs: []string{"old-id"}, Mounts: []Mount{mount}}}
	for _, requested := range [][]string{nil, {"old-name"}, {"old-id"}} {
		request := base
		request.Requested = requested
		got, err := Ownership(request)
		if err != nil || got.Node != "original" || got.NodeID != "old-id" || got.Mounts[0] != mount {
			t.Fatalf("binding = %+v, %v", got, err)
		}
	}
	changed := base
	changed.Requested = []string{"a-new"}
	if _, err := Ownership(changed); !errors.Is(err, ErrOwnership) {
		t.Fatalf("moving data: %v", err)
	}
	changed = base
	changed.Current = []Mount{{Type: "volume", Source: "empty", Target: "/data"}}
	if _, err := Ownership(changed); !errors.Is(err, ErrOwnership) {
		t.Fatalf("replacing data: %v", err)
	}
}

func TestFreshStorageSelectsReadyNonBuilder(t *testing.T) {
	request := OwnershipRequest{Region: "cn", Current: []Mount{{Type: "bind", Source: "/srv/data", Target: "/data"}}, Nodes: []Node{
		{Name: "a-builder", ID: "builder", Region: "cn", Ready: true, Builder: true},
		{Name: "b-worker", ID: "worker", Region: "cn", Ready: true},
	}}
	got, err := Ownership(request)
	if err != nil || got.Node != "b-worker" {
		t.Fatalf("binding = %+v, %v", got, err)
	}
	// An existing owner is not a fresh-placement candidate after loss of registration.
	request.Previous = Binding{Node: "b-worker", NodeID: "previous-node-id", Mounts: request.Current}
	request.PreviousRecord = true
	if _, err := Ownership(request); !errors.Is(err, ErrOwnership) {
		t.Fatalf("same-name replacement: %v", err)
	}
}
