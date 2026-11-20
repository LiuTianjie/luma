package sourcemap

import (
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/go-sourcemap/sourcemap"
	"github.com/liutianjie/luma-eye/internal/model"
)

func TestSymbolicateAndLRUCache(t *testing.T) {
	dir := t.TempDir()
	sym, err := NewSymbolicator(dir, 128)
	if err != nil {
		t.Fatal(err)
	}

	rawMap := []byte(`{
		"version": 3,
		"file": "index-abc.js",
		"sources": ["src/App.tsx"],
		"sourcesContent": ["const a = 1;\nconst b = 2;\nfunction test() {\n  handleClick();\n}\nexport default test;"],
		"names": ["handleClick"],
		"mappings": "AAGAA"
	}`)

	if _, err := sym.Upload("vibecheck", "v1.0.0", "assets/index-abc.js", rawMap); err != nil {
		t.Fatalf("upload failed: %v", err)
	}

	frames := []model.StackFrame{{
		Filename: "https://vibecheck.itool.tech/assets/index-abc.js",
		Function: "t",
		LineNo:   1,
		ColNo:    1,
		InApp:    true,
	}}
	out, ok := sym.Symbolicate("vibecheck", "v1.0.0", frames)
	if !ok || !out[0].Symbolicated {
		t.Fatalf("expected symbolicated frame, got %+v", out)
	}
	if out[0].Filename != "src/App.tsx" || out[0].LineNo != 4 || out[0].Function != "handleClick" {
		t.Fatalf("unexpected symbolicated frame: %+v", out[0])
	}
	if out[0].ContextLine != "  handleClick();" || len(out[0].PreContext) != 3 || len(out[0].PostContext) != 2 {
		t.Fatalf("expected source context lines, got %+v", out[0])
	}

	// Remove stored .zst and consumer map entry to prove LRU cache serves second lookup
	_ = os.RemoveAll(filepath.Join(dir, "sourcemaps"))
	sym.consumerMu.Lock()
	sym.consumers = map[string]*sourcemap.Consumer{}
	sym.consumerMu.Unlock()

	out2, ok2 := sym.Symbolicate("vibecheck", "v1.0.0", frames)
	if !ok2 || !out2[0].Symbolicated || out2[0].Filename != "src/App.tsx" {
		t.Fatalf("expected LRU cache hit after disk delete, got ok=%v out2=%+v", ok2, out2)
	}
}

func TestSymbolicateHermesBasenameAndIndexedSections(t *testing.T) {
	dir := t.TempDir()
	sym, err := NewSymbolicator(dir, 64)
	if err != nil {
		t.Fatal(err)
	}

	// Standard Metro/Hermes v3 source map uploaded by filename "main.jsbundle"
	rawMap := []byte(`{
		"version": 3,
		"sections": [{
			"offset": {"line": 0, "column": 0},
			"map": {
				"version": 3,
				"sources": ["src/screens/CheckoutScreen.tsx"],
				"sourcesContent": ["export function Checkout() {\n  return submitOrder();\n}\n"],
				"names": ["submitOrder"],
				"mappings": "AACAA"
			}
		}]
	}`)
	if _, err := sym.Upload("rnapp", "1.2.0", "main.jsbundle.map", rawMap); err != nil {
		t.Fatalf("upload failed: %v", err)
	}

	// Simulate iOS UUID sandbox bundle path with Hermes 0-based col=0 (stored as 1-based ColNo=1)
	frames := []model.StackFrame{{
		Filename: "/private/var/containers/Bundle/Application/99AA11BB/MyApp.app/main.jsbundle",
		Function: "anonymous",
		LineNo:   1,
		ColNo:    1,
		InApp:    true,
	}}
	out, ok := sym.Symbolicate("rnapp", "1.2.0", frames)
	if !ok || !out[0].Symbolicated {
		t.Fatalf("expected Hermes iOS sandbox frame symbolicated, got %+v", out)
	}
	if out[0].Filename != "src/screens/CheckoutScreen.tsx" || out[0].LineNo != 2 || out[0].Function != "submitOrder" {
		t.Fatalf("unexpected symbolicated Hermes frame: %+v", out[0])
	}
}

func TestSanitizeSegAndUploadInvalidatesLRUAndConcurrentSafe(t *testing.T) {
	if got := sanitizeSeg("."); got != "_" {
		t.Fatalf("expected sanitizeSeg('.') == '_', got %q", got)
	}
	if got := sanitizeSeg(".."); got != "_" {
		t.Fatalf("expected sanitizeSeg('..') == '_', got %q", got)
	}
	if got := sanitizeSeg("../etc"); strings.Contains(got, "..") || strings.Contains(got, "/") {
		t.Fatalf("expected sanitized segment without traversal, got %q", got)
	}

	dir := t.TempDir()
	sym, err := NewSymbolicator(dir, 64)
	if err != nil {
		t.Fatal(err)
	}

	rawV1 := []byte(`{
		"version": 3,
		"file": "app.js",
		"sources": ["src/v1.ts"],
		"sourcesContent": ["function v1Func() { throw new Error('v1'); }\n"],
		"names": ["v1Func"],
		"mappings": "AAAAA"
	}`)
	rawV2 := []byte(`{
		"version": 3,
		"file": "app.js",
		"sources": ["src/v2.ts"],
		"sourcesContent": ["function v2Func() { throw new Error('v2'); }\n"],
		"names": ["v2Func"],
		"mappings": "AAAAA"
	}`)

	// Upload with release "." must store under "_" subdirectory, not collapse into project dir.
	if _, err := sym.Upload("demo", ".", "app.js", rawV1); err != nil {
		t.Fatalf("upload with '.' release failed: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "sourcemaps", "demo", "_", "app.js.zst")); err != nil {
		t.Fatalf("expected sanitized '_' release dir, stat err=%v", err)
	}

	// Upload v1 for v1.0.0 and populate LRU cache
	if _, err := sym.Upload("demo", "v1.0.0", "assets/app.js", rawV1); err != nil {
		t.Fatal(err)
	}
	frames := []model.StackFrame{{Filename: "https://example.com/assets/app.js", LineNo: 1, ColNo: 1}}
	out1, ok1 := sym.Symbolicate("demo", "v1.0.0", frames)
	if !ok1 || out1[0].Filename != "src/v1.ts" || out1[0].Function != "v1Func" {
		t.Fatalf("expected v1 symbolication, got ok=%v frame=%+v", ok1, out1[0])
	}

	// Re-upload v2 for same project+release+asset -> LRU must be cleared immediately
	if _, err := sym.Upload("demo", "v1.0.0", "assets/app.js", rawV2); err != nil {
		t.Fatal(err)
	}
	out2, ok2 := sym.Symbolicate("demo", "v1.0.0", frames)
	if !ok2 || out2[0].Filename != "src/v2.ts" || out2[0].Function != "v2Func" {
		t.Fatalf("expected updated v2 symbolication after re-upload, got ok=%v frame=%+v", ok2, out2[0])
	}

	// Concurrent uploads to same target must not race on shared .tmp filename
	var wg sync.WaitGroup
	errs := make(chan error, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := sym.Upload("demo", "v1.0.0", "assets/app.js", rawV2); err != nil {
				errs <- err
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent upload failed: %v", err)
	}
}
