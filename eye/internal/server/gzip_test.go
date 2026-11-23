package server

import (
	"compress/gzip"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestWithGzipCompressesTextAndJSON(t *testing.T) {
	payload := strings.Repeat(`{"hello":"luma-eye","status":"ok"}`, 100)
	h := withGzip(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Header().Set("ETag", `"abc123"`)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(payload))
	}))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/test", nil)
	req.Header.Set("Accept-Encoding", "gzip, deflate, br")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d want 200", rec.Code)
	}
	if got := rec.Header().Get("Content-Encoding"); got != "gzip" {
		t.Fatalf("Content-Encoding=%q want gzip", got)
	}
	if got := rec.Header().Get("ETag"); got != `W/"abc123"` {
		t.Fatalf("ETag=%q want weak etag", got)
	}
	if !strings.Contains(rec.Header().Get("Vary"), "Accept-Encoding") {
		t.Fatalf("Vary=%q missing Accept-Encoding", rec.Header().Get("Vary"))
	}
	if rec.Body.Len() >= len(payload) {
		t.Fatalf("compressed len=%d not smaller than raw len=%d", rec.Body.Len(), len(payload))
	}

	gr, err := gzip.NewReader(rec.Body)
	if err != nil {
		t.Fatalf("gzip.NewReader: %v", err)
	}
	defer gr.Close()
	decoded, err := io.ReadAll(gr)
	if err != nil {
		t.Fatalf("io.ReadAll: %v", err)
	}
	if string(decoded) != payload {
		t.Fatalf("decoded payload mismatch")
	}
}

func TestWithGzipSkipsHeadBodyAndBinaryAndQ0(t *testing.T) {
	jsHandler := withGzip(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/javascript; charset=utf-8")
		w.Header().Set("Content-Length", "12345")
		w.WriteHeader(http.StatusOK)
		if r.Method != http.MethodHead {
			_, _ = w.Write([]byte("console.log('hi');"))
		}
	}))

	// HEAD must not emit gzip trailer bytes
	headReq := httptest.NewRequest(http.MethodHead, "/console/assets/app.js", nil)
	headReq.Header.Set("Accept-Encoding", "gzip")
	headRec := httptest.NewRecorder()
	jsHandler.ServeHTTP(headRec, headReq)
	if headRec.Header().Get("Content-Encoding") != "gzip" {
		t.Fatalf("HEAD Content-Encoding=%q want gzip", headRec.Header().Get("Content-Encoding"))
	}
	if headRec.Body.Len() != 0 {
		t.Fatalf("HEAD body len=%d want 0", headRec.Body.Len())
	}

	// q=0 must disable gzip
	q0Req := httptest.NewRequest(http.MethodGet, "/console/assets/app.js", nil)
	q0Req.Header.Set("Accept-Encoding", "gzip;q=0")
	q0Rec := httptest.NewRecorder()
	jsHandler.ServeHTTP(q0Rec, q0Req)
	if q0Rec.Header().Get("Content-Encoding") != "" {
		t.Fatalf("q=0 unexpectedly compressed: %q", q0Rec.Header().Get("Content-Encoding"))
	}

	// Binary image must not be compressed
	pngHandler := withGzip(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "image/png")
		_, _ = w.Write([]byte("\x89PNG\r\n\x1a\n"))
	}))
	pngReq := httptest.NewRequest(http.MethodGet, "/console/favicon.png", nil)
	pngReq.Header.Set("Accept-Encoding", "gzip")
	pngRec := httptest.NewRecorder()
	pngHandler.ServeHTTP(pngRec, pngReq)
	if pngRec.Header().Get("Content-Encoding") != "" {
		t.Fatalf("PNG unexpectedly compressed: %q", pngRec.Header().Get("Content-Encoding"))
	}
}

func TestEtagMatchesWeakAndStrong(t *testing.T) {
	if !etagMatches(`W/"abc123"`, `"abc123"`) {
		t.Fatalf("weak If-None-Match should match strong etag")
	}
	if !etagMatches(`"other", W/"abc123"`, `"abc123"`) {
		t.Fatalf("comma-separated If-None-Match should match")
	}
	if etagMatches(`"different"`, `"abc123"`) {
		t.Fatalf("non-matching etag should return false")
	}
}
