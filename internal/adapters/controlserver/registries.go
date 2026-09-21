package controlserver

import (
	"context"
	"encoding/json"
	"io"
	"net/http"

	"github.com/LiuTianjie/luma/internal/registry"
)

func (s Server) registryRequest(ctx context.Context, w http.ResponseWriter, r *http.Request, token string) bool {
	if r.URL.Path != "/v1/registries" && r.URL.Path != "/v1/registries/remove" {
		return false
	}
	if r.Method == "GET" && r.URL.Path == "/v1/registries" {
		items, err := s.Registries.RegistryCredentials(ctx)
		if err != nil {
			respondError(w, err)
		} else {
			write(w, 200, map[string]any{"registries": items})
		}
		return true
	}
	if r.Method != "POST" {
		return false
	}
	if !s.RegistryWrites {
		fail(w, 501, "not_migrated", "registry writes require writable Control mode")
		return true
	}
	var change registry.CredentialChange
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 2<<20))
	decoder.DisallowUnknownFields()
	var extra any
	if decoder.Decode(&change) != nil || decoder.Decode(&extra) != io.EOF {
		fail(w, 400, "invalid_body", "expected one bounded registry credential request")
		return true
	}
	result, err := s.Registries.ChangeRegistryCredential(ctx, token, change, r.URL.Path == "/v1/registries/remove")
	if err != nil {
		respondError(w, err)
	} else {
		write(w, 200, result)
	}
	return true
}
