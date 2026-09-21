package controlserver

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"

	builddomain "github.com/LiuTianjie/luma/internal/build"
)

func (s Server) localBuildRequest(ctx context.Context, w http.ResponseWriter, r *http.Request, token string) bool {
	if r.Method != "POST" || !strings.HasPrefix(r.URL.Path, "/v1/builds/local/") {
		return false
	}
	path := strings.TrimPrefix(r.URL.Path, "/v1/builds/local/")
	parts := strings.Split(path, "/")
	prepare := path == "prepare"
	if !prepare && (len(parts) != 2 || parts[0] == "" || (parts[1] != "complete" && parts[1] != "fail")) {
		return false
	}
	decode := func(value any) bool {
		d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8<<20))
		d.DisallowUnknownFields()
		var extra any
		if d.Decode(value) != nil || d.Decode(&extra) != io.EOF {
			fail(w, 400, "invalid_body", "expected one bounded local build request")
			return false
		}
		return true
	}
	if prepare {
		var request builddomain.LocalPrepareRequest
		if !decode(&request) {
			return true
		}
		result, err := s.Builds.PrepareLocal(ctx, token, request)
		if err != nil {
			respondError(w, err)
		} else {
			write(w, 200, result)
		}
		return true
	}
	if parts[1] == "fail" {
		var request struct {
			Message string `json:"message"`
		}
		if !decode(&request) {
			return true
		}
		run, replayed, err := s.Builds.Repository.FailLocalBuild(ctx, token, parts[0], request.Message)
		if err != nil {
			respondError(w, err)
		} else {
			write(w, 200, map[string]any{"run": run, "replayed": replayed})
		}
		return true
	}
	var request builddomain.LocalCompleteRequest
	if !decode(&request) {
		return true
	}
	run, err := s.Builds.CompleteLocal(ctx, token, parts[0], request)
	if err != nil {
		respondError(w, err)
		return true
	}
	return s.buildResponse(ctx, w, r, run, request.Queue)
}
