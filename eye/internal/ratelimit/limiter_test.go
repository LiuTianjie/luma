package ratelimit

import (
	"fmt"
	"testing"
	"time"
)

func TestTokenLimiterEvictsAtMaxBuckets(t *testing.T) {
	l := NewTokenLimiter(10, 20)
	l.maxBuckets = 32
	for i := 0; i < 200; i++ {
		l.Allow(fmt.Sprintf("ip:198.51.100.%d", i), 0)
	}
	l.mu.Lock()
	n := len(l.buckets)
	l.mu.Unlock()
	if n > 32 {
		t.Fatalf("expected TokenLimiter buckets <= 32, got %d", n)
	}
}

func TestStormSamplerEvictsAtMaxEntries(t *testing.T) {
	s := NewStormSampler(time.Minute, 3)
	s.maxEntries = 32
	now := time.Now().UTC()
	for i := 0; i < 200; i++ {
		s.Admit("demo", fmt.Sprintf("fp_%d", i), now, "sample")
	}
	s.mu.Lock()
	n := len(s.entries)
	s.mu.Unlock()
	if n > 32 {
		t.Fatalf("expected StormSampler entries <= 32, got %d", n)
	}
}

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
