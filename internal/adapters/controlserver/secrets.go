package controlserver

import (
	"context"
	"encoding/json"
	"github.com/LiuTianjie/luma/internal/credentials"
	"io"
	"net/http"
)

func (s Server) secretRequest(ctx context.Context, w http.ResponseWriter, r *http.Request, token string) bool {
	if r.URL.Path != "/v1/secrets" && r.URL.Path != "/v1/secrets/remove" {
		return false
	}
	if r.Method == "GET" && r.URL.Path == "/v1/secrets" {
		names, err := s.Secrets.SecretNames(ctx)
		if err != nil {
			respondError(w, err)
		} else {
			write(w, 200, map[string]any{"secrets": names})
		}
		return true
	}
	if r.Method != "POST" {
		return false
	}
	if !s.SecretWrites {
		fail(w, 501, "not_migrated", "secret writes require writable Control mode")
		return true
	}
	var change credentials.SecretChange
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 2<<20))
	decoder.DisallowUnknownFields()
	var extra any
	if decoder.Decode(&change) != nil || decoder.Decode(&extra) != io.EOF {
		fail(w, 400, "invalid_body", "expected one bounded secret request")
		return true
	}
	result, err := s.Secrets.ChangeSecret(ctx, token, change, r.URL.Path == "/v1/secrets/remove")
	if err != nil {
		respondError(w, err)
	} else {
		write(w, 200, result)
	}
	return true
}
