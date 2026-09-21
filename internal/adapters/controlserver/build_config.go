package controlserver

import (
	"context"
	"encoding/json"
	builddomain "github.com/LiuTianjie/luma/internal/build"
	"io"
	"net/http"
)

func (s Server) buildConfigRequest(ctx context.Context, w http.ResponseWriter, r *http.Request, token string) bool {
	if r.URL.Path != "/v1/builds/config" || (r.Method != "GET" && r.Method != "POST") {
		return false
	}
	var result builddomain.ConfigSummary
	var err error
	if r.Method == "GET" {
		result, err = s.BuildConfig.BuildConfig(ctx)
	} else {
		if !s.BuildConfigWrites {
			fail(w, 501, "read_only", "build configuration writes require writable Control mode")
			return true
		}
		var change builddomain.ConfigChange
		d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
		d.DisallowUnknownFields()
		var extra any
		if d.Decode(&change) != nil || d.Decode(&extra) != io.EOF {
			fail(w, 400, "invalid_body", "expected one bounded build configuration request")
			return true
		}
		result, err = s.BuildConfig.ChangeBuildConfig(ctx, token, change)
	}
	if err != nil {
		respondError(w, err)
	} else {
		write(w, 200, map[string]any{"build": result})
	}
	return true
}
