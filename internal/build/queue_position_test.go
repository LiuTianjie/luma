package build

import "testing"

func TestPositionUsesProjectOwnersAndFIFOButExcludesCallerUploads(t *testing.T) {
	project, other := "repo", "another"
	current := QueueEntry{ID: "current", Status: "queued", Project: &project, Order: 4}
	entries := []QueueEntry{
		current,
		{ID: "foreign", Status: "running", Project: &other},
		{ID: "caller-upload", Status: "running", Mode: "local", CallerQueued: true, Project: &project},
		{ID: "finished", Status: "failed", Executing: true, Project: &project},
		{ID: "later", Status: "queued", Project: &project, Order: 5},
		{ID: "older", Status: "queued", Project: &project, Order: 2},
		{ID: "canceling", Status: "canceling", Project: &project, Order: 1},
		{ID: "owner", Status: "running", Project: &project, Order: 0},
		{ID: "queued-owner", Status: "queued", Executing: true, Project: &project, Order: 9},
	}
	for range 2 {
		counter := NewPositionCounter(current)
		for _, entry := range entries {
			counter.Add(entry)
		}
		result := counter.Result()
		if result.QueuePosition != 5 || result.WaitingFor != "owner" {
			t.Fatalf("%+v", result)
		}
		for i, j := 0, len(entries)-1; i < j; i, j = i+1, j-1 {
			entries[i], entries[j] = entries[j], entries[i]
		}
	}
}
func TestPositionTiesNullProjectAndTerminal(t *testing.T) {
	counter := NewPositionCounter(QueueEntry{ID: "b", Status: "queued", Order: 1})
	counter.Add(QueueEntry{ID: "a", Status: "queued", Order: 1})
	empty := ""
	counter.Add(QueueEntry{ID: "unrelated-empty", Status: "running", Project: &empty})
	if result := counter.Result(); result.QueuePosition != 2 || result.WaitingFor != "a" {
		t.Fatalf("%+v", result)
	}
	if NewPositionCounter(QueueEntry{Status: "succeeded"}).Result() != nil {
		t.Fatal("terminal build has queue position")
	}
}
