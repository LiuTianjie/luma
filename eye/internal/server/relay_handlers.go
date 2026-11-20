package server

import (
	"crypto/subtle"
	"encoding/json"
	"io"
	"net/http"

	"github.com/liutianjie/luma-eye/internal/model"
	"github.com/liutianjie/luma-eye/internal/pipeline"
)

func (s *Server) isRelayAuthorized(r *http.Request) bool {
	tok := extractBearer(r)
	if tok == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(tok), []byte(s.cfg.RelaySecret)) == 1
}

func (s *Server) handleRelayIngest(w http.ResponseWriter, r *http.Request) {
	if !s.isRelayAuthorized(r) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, 16<<20))
	if err != nil {
		http.Error(w, "read failed", http.StatusBadRequest)
		return
	}
	raw, err = pipeline.DecompressBody(r.Header.Get("Content-Encoding"), raw)
	if err != nil {
		http.Error(w, "decompress failed", http.StatusBadRequest)
		return
	}
	var batch model.RelayBatch
	if err := json.Unmarshal(raw, &batch); err != nil {
		http.Error(w, "invalid relay json", http.StatusBadRequest)
		return
	}
	if s.batcher != nil && !s.batcher.Enqueue(batch) {
		w.Header().Set("Retry-After", "5")
		http.Error(w, "relay queue overloaded, retry later", http.StatusServiceUnavailable)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) handleProjectsSync(w http.ResponseWriter, r *http.Request) {
	if !s.isRelayAuthorized(r) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"projects": s.store.List(),
	})
}
