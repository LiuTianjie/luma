package pipeline

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/klauspost/compress/zstd"
	"github.com/liutianjie/luma-eye/internal/config"
	"github.com/liutianjie/luma-eye/internal/model"
	"github.com/liutianjie/luma-eye/internal/observe"
	"github.com/liutianjie/luma-eye/internal/project"
	"github.com/liutianjie/luma-eye/internal/scrub"
)

func TestParseStackAndFingerprint(t *testing.T) {
	v8Stack := `TypeError: Cannot read properties of undefined (reading 'id')
    at handleSubmit (https://vibecheck.itool.tech/assets/index-123.js:1:4096)
    at onClick (https://vibecheck.itool.tech/assets/index-123.js:1:2048)`
	frames := ParseStackString(v8Stack)
	if len(frames) != 2 {
		t.Fatalf("expected 2 frames, got %d", len(frames))
	}
	// Ordered oldest-to-newest
	if frames[0].Function != "onClick" || frames[1].Function != "handleSubmit" {
		t.Fatalf("unexpected frame ordering: %+v", frames)
	}
	if ComputeCulprit(frames) != "index-123.js:1 in handleSubmit" {
		t.Fatalf("unexpected culprit: %s", ComputeCulprit(frames))
	}
	fp1 := ComputeFingerprint("TypeError", "Order 12345 failed for user 999", frames)
	fp2 := ComputeFingerprint("TypeError", "Order 98765 failed for user 111", frames)
	if fp1 != fp2 {
		t.Fatalf("expected normalized fingerprints to match: %s vs %s", fp1, fp2)
	}
}

func TestParseHermesStackFrames(t *testing.T) {
	hermesStack := `Error: checkout failed
    at handleCheckout (address at /private/var/containers/Bundle/Application/1234/App.app/main.jsbundle:1:0)
    at anonymous (address at index.android.bundle:1:4096)
    at apply (native)
    at callFunctionReturnFlushedQueue (InternalBytecode.js:1:1024)`
	frames := ParseStackString(hermesStack)
	if len(frames) != 3 {
		t.Fatalf("expected 3 frames (skipping native), got %d: %+v", len(frames), frames)
	}
	// Oldest-to-newest: InternalBytecode.js -> index.android.bundle -> main.jsbundle
	if frames[0].InApp {
		t.Fatalf("expected InternalBytecode.js InApp=false, got %+v", frames[0])
	}
	if frames[1].Filename != "index.android.bundle" || frames[1].ColNo != 4097 {
		t.Fatalf("expected index.android.bundle 1-based col 4097, got %+v", frames[1])
	}
	if frames[2].Filename != "/private/var/containers/Bundle/Application/1234/App.app/main.jsbundle" || frames[2].Function != "handleCheckout" || frames[2].LineNo != 1 || frames[2].ColNo != 1 || !frames[2].InApp {
		t.Fatalf("unexpected top Hermes frame: %+v", frames[2])
	}
	if got := ComputeCulprit(frames); got != "main.jsbundle:1 in handleCheckout" {
		t.Fatalf("unexpected Hermes culprit: %q", got)
	}
}

func TestSentryEnvelopeGzipLengthAndNewline(t *testing.T) {
	eventJSON := `{"event_id":"abc123def456","timestamp":1700000000.5,"level":"error","environment":"production","release":"sha-1","exception":{"values":[{"type":"ReferenceError","value":"foo is not defined","stacktrace":{"frames":[{"filename":"https://app.itool.tech/main.js","function":"render","lineno":10,"colno":5,"in_app":true}]}}]},"breadcrumbs":[{"category":"ui.click","level":"info","message":"button#submit","timestamp":1700000000.0}]}`
	envHeader := `{"event_id":"abc123def456","dsn":"https://pk_vibecheck_1234@eye.itool.tech/1"}`
	itemHeader := `{"type":"event","length":` + itoa(len(eventJSON)) + `}`
	rawEnvelope := envHeader + "\n" + itemHeader + "\n" + eventJSON + "\n"

	var gzBuf bytes.Buffer
	gw := gzip.NewWriter(&gzBuf)
	_, _ = gw.Write([]byte(rawEnvelope))
	_ = gw.Close()

	// Decompress without Content-Encoding header (gzip magic bytes auto-detection)
	decomp, err := DecompressBody("", gzBuf.Bytes())
	if err != nil {
		t.Fatalf("decompress failed: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/1/envelope/", bytes.NewReader(decomp))
	if gotKey := ExtractSentryKey(req, decomp); gotKey != "pk_vibecheck_1234" {
		t.Fatalf("expected sentry key pk_vibecheck_1234, got %q", gotKey)
	}

	res, err := ParseSentryEnvelope("vibecheck", scrub.UAInfo{Browser: "Chrome", OS: "macOS", DeviceType: "desktop"}, decomp)
	if err != nil {
		t.Fatalf("parse envelope failed: %v", err)
	}
	if res.EventID != "abc123def456" || len(res.Exceptions) != 1 {
		t.Fatalf("unexpected parse result: %+v", res)
	}
	if len(res.Exceptions[0].Breadcrumbs) != 1 || res.Exceptions[0].Frames[0].Function != "render" {
		t.Fatalf("unexpected parsed exception: %+v", res.Exceptions[0])
	}
}

func TestEdgeToCoreZstdRelayRoundtrip(t *testing.T) {
	var mu sync.Mutex
	var received model.RelayBatch

	zstdDec, _ := zstd.NewReader(nil)
	defer zstdDec.Close()

	coreSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer relay-secret" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/internal/v1/projects-sync":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"projects": []*model.Project{{DSNID: 1, Slug: "vibecheck", PublicKey: "pk_test", RateLimitRPS: 200}},
			})
		case "/internal/v1/relay":
			var buf bytes.Buffer
			_, _ = buf.ReadFrom(r.Body)
			raw, err := zstdDec.DecodeAll(buf.Bytes(), nil)
			if err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			mu.Lock()
			_ = json.Unmarshal(raw, &received)
			mu.Unlock()
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	defer coreSrv.Close()

	store, _ := project.NewStore(t.TempDir(), false)
	metrics := observe.NewMetrics()
	cfg := &config.Config{
		Mode:          config.ModeEdge,
		CoreURL:       coreSrv.URL,
		RelaySecret:   "relay-secret",
		BatchInterval: 50 * time.Millisecond,
		BatchSize:     100,
		QueueCapacity: 100,
	}
	batcher, err := NewBatcher(cfg, store, nil, nil, nil, metrics)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	batcher.Start(ctx)

	batcher.Enqueue(model.RelayBatch{
		Events: []model.Event{{
			TS:        model.FormatTS(time.Now().UTC()),
			Project:   "vibecheck",
			EventType: "pageview",
			EventName: "pageview",
			ActorID:   "u1",
		}},
	})

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		n := len(received.Events)
		mu.Unlock()
		if n == 1 {
			break
		}
		time.Sleep(30 * time.Millisecond)
	}
	cancel()
	shutCtx, sCancel := context.WithTimeout(context.Background(), time.Second)
	batcher.Shutdown(shutCtx)
	sCancel()

	mu.Lock()
	defer mu.Unlock()
	if len(received.Events) != 1 || received.Events[0].Project != "vibecheck" {
		t.Fatalf("expected relayed event, got %+v", received)
	}
}

func itoa(n int) string {
	b, _ := json.Marshal(n)
	return string(b)
}
