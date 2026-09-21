package deployment

import "testing"

func TestHealthyAllocationsBelongToSubmittedVersion(t *testing.T) {
	oldVersion, version := uint64(3), uint64(4)
	current := Allocation{ID: "current", JobID: "app", Group: "web", Version: &version, Status: "running", Desired: "run", Tasks: map[string]TaskState{"web": {State: "running"}}}
	old := current
	old.ID, old.Version = "old", &oldVersion
	groups := map[string]int{"web": 1}
	if healthy, _ := AllocationsHealthy([]Allocation{old}, "app", version, groups); healthy {
		t.Fatal("old version acknowledged new submission")
	}
	if healthy, count := AllocationsHealthy([]Allocation{old, current}, "app", version, groups); !healthy || count != 1 {
		t.Fatalf("current version: %v %d", healthy, count)
	}
	current.Tasks = map[string]TaskState{"web": {State: "pending"}}
	if healthy, _ := AllocationsHealthy([]Allocation{current}, "app", version, groups); healthy {
		t.Fatal("pending task reported healthy")
	}
}
