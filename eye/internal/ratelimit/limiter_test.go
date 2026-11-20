package ratelimit

import (
	"testing"
	"time"
)

func TestStormSampler(t *testing.T) {
	s := NewStormSampler(time.Minute, 3)
	now := time.Now().UTC()

	for i := 0; i < 10; i++ {
		admitted := s.Admit("vibecheck", "fp1", now, "sample")
		if i < 3 && !admitted {
			t.Fatalf("expected first 3 occurrences to be admitted (i=%d)", i)
		}
		if i >= 3 && admitted {
			t.Fatalf("expected occurrence %d to be collapsed", i)
		}
	}

	collapsed := s.DrainCollapsed(now)
	if len(collapsed) != 1 || collapsed[0].Count != 7 {
		t.Fatalf("expected 1 collapsed entry with count 7, got %+v", collapsed)
	}

	if again := s.DrainCollapsed(now); len(again) != 0 {
		t.Fatalf("expected empty second drain, got %+v", again)
	}
}
