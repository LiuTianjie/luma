package project

import (
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
