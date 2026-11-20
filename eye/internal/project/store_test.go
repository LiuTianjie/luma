package project

import (
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/liutianjie/luma-eye/internal/model"
)

func TestStoreAutoProvisionAndOrigin(t *testing.T) {
	dir := t.TempDir()
	s, err := NewStore(dir, false)
	if err != nil {
		t.Fatal(err)
	}

	// Reject unknown key without autoKey
	if _, err := s.ResolveForIngest("vibecheck", "wrong", "https://vibecheck.itool.tech", "auto_123", []string{".itool.tech"}); err == nil {
		t.Fatalf("expected error for wrong key")
	}

	// Reject bad origin suffix during auto-provision
	if _, err := s.ResolveForIngest("vibecheck", "auto_123", "https://evil.com", "auto_123", []string{".itool.tech"}); err != ErrOriginRejected {
		t.Fatalf("expected ErrOriginRejected, got %v", err)
	}

	// Auto-provision allowed origin suffix
	p, err := s.ResolveForIngest("vibecheck", "auto_123", "https://vibecheck.itool.tech", "auto_123", []string{".itool.tech"})
	if err != nil {
		t.Fatalf("auto provision failed: %v", err)
	}
	if p.Slug != "vibecheck" || p.DSNID != 1 {
		t.Fatalf("unexpected project: %+v", p)
	}

	// Reload from disk and verify persistence
	s2, err := NewStore(dir, false)
	if err != nil {
		t.Fatal(err)
	}
	p2, ok := s2.Get("1")
	if !ok || p2.PublicKey != p.PublicKey {
		t.Fatalf("reload failed: ok=%v p2=%+v", ok, p2)
	}

	// Issue state transitions: new -> resolved -> regression
	now := time.Now().UTC()
	alert, reason, _ := s2.CheckAndRecordIssue("vibecheck", "fp123", now)
	if !alert || reason != "new_issue" {
		t.Fatalf("expected new_issue alert, got alert=%v reason=%s", alert, reason)
	}
	alert, _, _ = s2.CheckAndRecordIssue("vibecheck", "fp123", now.Add(time.Second))
	if alert {
		t.Fatalf("did not expect duplicate alert for already-open issue")
	}
	if _, err := s2.SetIssueStatus("vibecheck", "fp123", model.IssueResolved); err != nil {
		t.Fatal(err)
	}
	alert, reason, st := s2.CheckAndRecordIssue("vibecheck", "fp123", now.Add(2*time.Second))
	if !alert || reason != "regression" || st.Status != model.IssueOpen {
		t.Fatalf("expected regression alert & reopen, got alert=%v reason=%s st=%+v", alert, reason, st)
	}
}

func TestStoreConcurrentIssuePersistenceAndResolveForIngest(t *testing.T) {
	dir := t.TempDir()
	s, err := NewStore(dir, false)
	if err != nil {
		t.Fatal(err)
	}
	proj, _, err := s.EnsureProject("demo", "Demo", []string{"https://demo.example.com"}, 200)
	if err != nil {
		t.Fatal(err)
	}

	var wg sync.WaitGroup
	now := time.Now().UTC()

	// Concurrent writers updating and flushing issue state via ioMu outside s.mu
	for g := 0; g < 4; g++ {
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			for i := 0; i < 15; i++ {
				fp := fmt.Sprintf("fp_%d_%d", worker, i)
				s.CheckAndRecordIssue("demo", fp, now)
				if i%3 == 0 {
					_, _ = s.SetIssueStatus("demo", fp, model.IssueResolved)
				}
			}
		}(g)
	}

	// Concurrent ingest readers calling ResolveForIngest continuously
	readErrs := make(chan error, 8)
	for g := 0; g < 4; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 100; i++ {
				if _, err := s.ResolveForIngest("demo", proj.PublicKey, "https://demo.example.com", "", nil); err != nil {
					readErrs <- err
					return
				}
			}
		}()
	}

	wg.Wait()
	close(readErrs)
	for err := range readErrs {
		t.Fatalf("ResolveForIngest failed under concurrent issue writes: %v", err)
	}

	// Reload from disk to confirm atomic persistence is intact and contains all 60 issues
	s2, err := NewStore(dir, false)
	if err != nil {
		t.Fatalf("reload store failed: %v", err)
	}
	states := s2.GetIssueStates("demo")
	if len(states) != 60 {
		t.Fatalf("expected 60 persisted issues after reload, got %d", len(states))
	}
}
