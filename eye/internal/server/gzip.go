package server

import (
	"compress/gzip"
	"io"
	"mime"
	"net/http"
	"strconv"
	"strings"
	"sync"
)

var gzipWriterPool = sync.Pool{
	New: func() any {
		w, _ := gzip.NewWriterLevel(io.Discard, gzip.DefaultCompression)
		return w
	},
}

// withGzip transparently gzip-compresses compressible HTTP responses (JS, CSS, HTML,
// JSON, SVG, plain text, etc.) when the client advertises gzip support.
func withGzip(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Upgrade") != "" || r.Header.Get("Range") != "" || !acceptsGzip(r) {
			next.ServeHTTP(w, r)
			return
		}
		gw := &gzipResponseWriter{ResponseWriter: w}
		defer gw.close()
		next.ServeHTTP(gw, r)
	})
}

type gzipResponseWriter struct {
	http.ResponseWriter
	gz          *gzip.Writer
	decided     bool
	compressing bool
}

func (g *gzipResponseWriter) decide(status int, sample []byte) {
	if g.decided {
		return
	}
	g.decided = true

	if status < http.StatusOK || status == http.StatusNoContent || status == http.StatusNotModified {
		return
	}
	h := g.Header()
	if h.Get("Content-Encoding") != "" {
		return
	}

	ct := h.Get("Content-Type")
	if ct == "" && len(sample) > 0 {
		ct = http.DetectContentType(sample)
		h.Set("Content-Type", ct)
	}
	if !isCompressibleContentType(ct) {
		return
	}

	g.compressing = true
	h.Del("Content-Length")
	h.Set("Content-Encoding", "gzip")
	appendVary(h, "Accept-Encoding")
	if etag := strings.TrimSpace(h.Get("ETag")); etag != "" && !strings.HasPrefix(etag, "W/") {
		h.Set("ETag", "W/"+etag)
	}
}

func (g *gzipResponseWriter) WriteHeader(status int) {
	if !g.decided {
		g.decide(status, nil)
	}
	g.ResponseWriter.WriteHeader(status)
}

func (g *gzipResponseWriter) Write(p []byte) (int, error) {
	if !g.decided {
		g.decide(http.StatusOK, p)
	}
	if !g.compressing {
		return g.ResponseWriter.Write(p)
	}
	if g.gz == nil {
		gz := gzipWriterPool.Get().(*gzip.Writer)
		gz.Reset(g.ResponseWriter)
		g.gz = gz
	}
	return g.gz.Write(p)
}

func (g *gzipResponseWriter) Flush() {
	if g.gz != nil {
		_ = g.gz.Flush()
	}
	if f, ok := g.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func (g *gzipResponseWriter) Unwrap() http.ResponseWriter {
	return g.ResponseWriter
}

func (g *gzipResponseWriter) close() {
	if g.gz != nil {
		_ = g.gz.Close()
		g.gz.Reset(io.Discard)
		gzipWriterPool.Put(g.gz)
		g.gz = nil
	}
}

func acceptsGzip(r *http.Request) bool {
	for _, header := range r.Header.Values("Accept-Encoding") {
		for _, part := range strings.Split(header, ",") {
			token := strings.TrimSpace(part)
			if token == "" {
				continue
			}
			enc, params, hasParams := strings.Cut(token, ";")
			if !strings.EqualFold(strings.TrimSpace(enc), "gzip") {
				continue
			}
			if !hasParams {
				return true
			}
			q := 1.0
			for _, param := range strings.Split(params, ";") {
				k, v, ok := strings.Cut(strings.TrimSpace(param), "=")
				if ok && strings.EqualFold(strings.TrimSpace(k), "q") {
					if parsed, err := strconv.ParseFloat(strings.TrimSpace(v), 64); err == nil {
						q = parsed
					}
				}
			}
			if q > 0 {
				return true
			}
		}
	}
	return false
}

func isCompressibleContentType(ct string) bool {
	if ct == "" {
		return false
	}
	mediaType, _, err := mime.ParseMediaType(ct)
	if err != nil {
		mediaType = strings.TrimSpace(strings.Split(ct, ";")[0])
	}
	mediaType = strings.ToLower(mediaType)
	if mediaType == "text/event-stream" {
		return false
	}
	if strings.HasPrefix(mediaType, "text/") {
		return true
	}
	switch mediaType {
	case "application/javascript",
		"application/x-javascript",
		"application/json",
		"application/manifest+json",
		"application/xml",
		"application/wasm",
		"image/svg+xml":
		return true
	default:
		return strings.HasSuffix(mediaType, "+json") || strings.HasSuffix(mediaType, "+xml")
	}
}

func appendVary(h http.Header, value string) {
	for _, existing := range h.Values("Vary") {
		for _, part := range strings.Split(existing, ",") {
			if strings.EqualFold(strings.TrimSpace(part), value) {
				return
			}
		}
	}
	h.Add("Vary", value)
}

func etagMatches(ifNoneMatch, etag string) bool {
	target := strings.TrimPrefix(strings.TrimSpace(etag), "W/")
	if target == "" {
		return false
	}
	for _, candidate := range strings.Split(ifNoneMatch, ",") {
		c := strings.TrimSpace(candidate)
		if c == "*" {
			return true
		}
		if strings.TrimPrefix(c, "W/") == target {
			return true
		}
	}
	return false
}
