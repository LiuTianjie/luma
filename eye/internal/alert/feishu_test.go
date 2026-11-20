package alert

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/liutianjie/luma-eye/internal/model"
)

func TestFeishuNotifierBoundedConcurrencyAndMapCap(t *testing.T) {
	var inFlight atomic.Int32
	var maxInFlight atomic.Int32
	var delivered atomic.Int32

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cur := inFlight.Add(1)
		for {
			prev := maxInFlight.Load()
			if cur <= prev || maxInFlight.CompareAndSwap(prev, cur) {
				break
			}
		}
		time.Sleep(30 * time.Millisecond)
		inFlight.Add(-1)
		delivered.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	n := NewFeishuNotifier(srv.URL, "https://eye.example.com")
	n.maxEntries = 32

	// Fire a burst of 80 distinct alerts concurrently: goroutines must stay capped at feishuMaxConcurrent (16)
	for i := 0; i < 80; i++ {
		n.NotifyIssueAsync("new_issue", model.ExceptionEvent{
			Project:      "demo",
			Fingerprint:  fmt.Sprintf("fp_burst_%d", i),
			ErrorType:    "TypeError",
			ErrorMessage: "boom",
		})
	}

	// Wait for in-flight requests to finish
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if inFlight.Load() == 0 && delivered.Load() > 0 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if maxInFlight.Load() > feishuMaxConcurrent {
		t.Fatalf("expected max concurrent webhooks <= %d, got %d", feishuMaxConcurrent, maxInFlight.Load())
	}
	if delivered.Load() == 0 {
		t.Fatalf("expected at least some alerts delivered")
	}

	// Verify lastSent map respects maxEntries cap even across many sequential waves
	fastSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer fastSrv.Close()

	n2 := NewFeishuNotifier(fastSrv.URL, "https://eye.example.com")
	n2.maxEntries = 20
	// Seed expired entries to verify expired eviction path too
	n2.mu.Lock()
	for i := 0; i < 15; i++ {
		n2.lastSent[fmt.Sprintf("expired_%d", i)] = time.Now().Add(-2 * time.Minute)
	}
	n2.mu.Unlock()

	for i := 0; i < 60; i++ {
		n2.NotifyIssueAsync("new_issue", model.ExceptionEvent{
			Project:     "demo",
			Fingerprint: fmt.Sprintf("fp_seq_%d", i),
			ErrorType:   "Error",
		})
		// Wait briefly for semaphore slot to release
		time.Sleep(2 * time.Millisecond)
	}

	n2.mu.Lock()
	sz := len(n2.lastSent)
	n2.mu.Unlock()
	if sz > n2.maxEntries {
		t.Fatalf("expected lastSent size <= %d, got %d", n2.maxEntries, sz)
	}
}
