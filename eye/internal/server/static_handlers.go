package server

import (
	"crypto/sha256"
	"encoding/hex"
	"io/fs"
	"mime"
	"net/http"
	"path"
	"strings"

	"github.com/liutianjie/luma-eye/internal/console"
	"github.com/liutianjie/luma-eye/internal/sdkasset"
)

var (
	eyeMinETag       string
	eyeReplayMinETag string
)

func init() {
	_ = mime.AddExtensionType(".woff2", "font/woff2")
	h1 := sha256.Sum256(sdkasset.EyeMinJS)
	eyeMinETag = `"` + hex.EncodeToString(h1[:8]) + `"`
	h2 := sha256.Sum256(sdkasset.EyeReplayMinJS)
	eyeReplayMinETag = `"` + hex.EncodeToString(h2[:8]) + `"`
}

func serveJSBundle(w http.ResponseWriter, r *http.Request, body []byte, etag string) {
	w.Header().Set("Content-Type", "application/javascript; charset=utf-8")
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Cache-Control", "public, max-age=300, stale-while-revalidate=86400")
	w.Header().Set("ETag", etag)
	if strings.TrimSpace(r.Header.Get("If-None-Match")) == etag {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(body)
}

func (s *Server) handleSDKAsset(w http.ResponseWriter, r *http.Request) {
	serveJSBundle(w, r, sdkasset.EyeMinJS, eyeMinETag)
}

func (s *Server) handleReplaySDKAsset(w http.ResponseWriter, r *http.Request) {
	serveJSBundle(w, r, sdkasset.EyeReplayMinJS, eyeReplayMinETag)
}

func (s *Server) handleConsoleSPA(w http.ResponseWriter, r *http.Request) {
	sub, err := fs.Sub(console.DistFS, "dist")
	if err != nil {
		http.Error(w, "console assets unavailable", http.StatusInternalServerError)
		return
	}

	reqPath := strings.TrimPrefix(r.URL.Path, "/console/")
	reqPath = strings.TrimPrefix(path.Clean("/"+reqPath), "/")
	if reqPath == "" || reqPath == "." {
		reqPath = "index.html"
	}

	if reqPath != "index.html" {
		if info, err := fs.Stat(sub, reqPath); err == nil && !info.IsDir() {
			if strings.HasPrefix(reqPath, "assets/") {
				w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
			} else {
				w.Header().Set("Cache-Control", "no-cache")
			}
			rCopy := r.Clone(r.Context())
			rCopy.URL.Path = "/" + reqPath
			http.FileServer(http.FS(sub)).ServeHTTP(w, rCopy)
			return
		}
	}

	// SPA fallback to index.html
	indexBytes, err := fs.ReadFile(sub, "index.html")
	if err != nil {
		http.Error(w, "console index missing", http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(indexBytes)
}
