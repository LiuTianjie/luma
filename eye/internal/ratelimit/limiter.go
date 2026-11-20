package ratelimit

import (
	"sync"
	"time"
)

const (
	defaultMaxTokenBuckets = 65536
	defaultMaxStormEntries = 4096
)

type bucket struct {
	tokens float64
	last   time.Time
}

type TokenLimiter struct {
	mu         sync.Mutex
	buckets    map[string]*bucket
	maxBuckets int
	burst      float64
	rate       float64
	lastGC     time.Time
}

func NewTokenLimiter(ratePerSec, burst float64) *TokenLimiter {
	return &TokenLimiter{
		buckets:    make(map[string]*bucket),
		maxBuckets: defaultMaxTokenBuckets,
		rate:       ratePerSec,
		burst:      burst,
		lastGC:     time.Now(),
	}
}

func (l *TokenLimiter) Allow(key string, rateOverride float64) bool {
	now := time.Now()
	rate := l.rate
	burst := l.burst
	if rateOverride > 0 {
		rate = rateOverride
		burst = rateOverride * 2
		if burst < 10 {
			burst = 10
		}
	}

	l.mu.Lock()
	defer l.mu.Unlock()

	if now.Sub(l.lastGC) > 5*time.Minute {
		cutoff := now.Add(-5 * time.Minute)
		for k, b := range l.buckets {
			if b.last.Before(cutoff) {
				delete(l.buckets, k)
			}
		}
		l.lastGC = now
	}

	b, ok := l.buckets[key]
	if !ok {
		l.evictIfFullLocked(now)
		b = &bucket{tokens: burst - 1, last: now}
		l.buckets[key] = b
		return true
	}

	elapsed := now.Sub(b.last).Seconds()
	b.tokens += elapsed * rate
	if b.tokens > burst {
		b.tokens = burst
	}
	b.last = now
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}

func (l *TokenLimiter) evictIfFullLocked(now time.Time) {
	maxBuckets := l.maxBuckets
	if maxBuckets <= 0 {
		maxBuckets = defaultMaxTokenBuckets
	}
	if len(l.buckets) < maxBuckets {
		return
	}
	// First sweep stale entries idle > 5 minutes.
	cutoff := now.Add(-5 * time.Minute)
	for k, b := range l.buckets {
		if b.last.Before(cutoff) {
			delete(l.buckets, k)
		}
	}
	l.lastGC = now
	if len(l.buckets) < maxBuckets {
		return
	}
	// Next sweep entries idle > 30 seconds down to target watermark.
	target := maxBuckets - max(1, maxBuckets/8)
	warmCutoff := now.Add(-30 * time.Second)
	for k, b := range l.buckets {
		if len(l.buckets) <= target {
			return
		}
		if b.last.Before(warmCutoff) {
			delete(l.buckets, k)
		}
	}
	// Finally evict arbitrary entries down to target so amortized cost stays O(1).
	for k := range l.buckets {
		if len(l.buckets) <= target {
			return
		}
		delete(l.buckets, k)
	}
}

// StormEntry tracks dropped excess occurrences for a (project, fingerprint) pair
// within the current sampling window so error storms collapse into weighted summaries.
type StormEntry struct {
	WindowStart time.Time
	Count       uint32
	Suppressed  uint32
	Sample      any
}

type StormSampler struct {
	mu         sync.Mutex
	window     time.Duration
	maxFull    uint32
	maxEntries int
	entries    map[string]*StormEntry
}

func NewStormSampler(window time.Duration, maxFullPerWindow uint32) *StormSampler {
	if window <= 0 {
		window = time.Minute
	}
	if maxFullPerWindow == 0 {
		maxFullPerWindow = 10
	}
	return &StormSampler{
		window:     window,
		maxFull:    maxFullPerWindow,
		maxEntries: defaultMaxStormEntries,
		entries:    make(map[string]*StormEntry),
	}
}

// Admit returns true if the full exception event should be emitted immediately.
// If false, the caller skips storing a full row and stores a collapsed weight inside StormSampler.
func (s *StormSampler) Admit(project, fingerprint string, now time.Time, sample any) bool {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	key := project + "\x00" + fingerprint

	s.mu.Lock()
	defer s.mu.Unlock()

	e, ok := s.entries[key]
	if !ok || now.Sub(e.WindowStart) >= s.window {
		if !ok {
			s.evictIfFullLocked(now)
		}
		s.entries[key] = &StormEntry{
			WindowStart: now,
			Count:       1,
			Sample:      sample,
		}
		return true
	}
	e.Count++
	if e.Count <= s.maxFull {
		return true
	}
	e.Suppressed++
	if e.Sample == nil && sample != nil {
		e.Sample = sample
	}
	return false
}

func (s *StormSampler) evictIfFullLocked(now time.Time) {
	maxEntries := s.maxEntries
	if maxEntries <= 0 {
		maxEntries = defaultMaxStormEntries
	}
	if len(s.entries) < maxEntries {
		return
	}
	// First sweep expired entries with no pending suppressed count.
	for k, e := range s.entries {
		if now.Sub(e.WindowStart) >= s.window && e.Suppressed == 0 {
			delete(s.entries, k)
		}
	}
	if len(s.entries) < maxEntries {
		return
	}
	target := maxEntries - max(1, maxEntries/8)
	// Next prefer evicting entries without pending suppressed counts.
	for k, e := range s.entries {
		if len(s.entries) <= target {
			return
		}
		if e.Suppressed == 0 {
			delete(s.entries, k)
		}
	}
	// Finally evict remaining entries down to target.
	for k := range s.entries {
		if len(s.entries) <= target {
			return
		}
		delete(s.entries, k)
	}
}

// DrainCollapsed returns suppressed counts accumulated so far (resetting suppressed counters to 0)
// so the batcher can persist an accurate weighted summary row without spamming storage.
func (s *StormSampler) DrainCollapsed(now time.Time) []StormEntry {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	var out []StormEntry
	for k, e := range s.entries {
		if e.Suppressed > 0 {
			out = append(out, StormEntry{
				WindowStart: e.WindowStart,
				Count:       e.Suppressed,
				Suppressed:  e.Suppressed,
				Sample:      e.Sample,
			})
			e.Suppressed = 0
		}
		if now.Sub(e.WindowStart) >= s.window*2 && e.Suppressed == 0 {
			delete(s.entries, k)
		}
	}
	return out
}
