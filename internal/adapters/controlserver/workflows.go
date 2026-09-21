package controlserver

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/LiuTianjie/luma/internal/workflow"
)

func (s Server) workflowRequest(ctx context.Context, w http.ResponseWriter, r *http.Request, token string) bool {
	var result any
	var err error
	path := r.URL.EscapedPath()
	switch {
	case r.Method == http.MethodGet && path == "/v1/workflows":
		var rows []workflow.Record
		rows, err = s.Workflows.List(ctx)
		result = map[string]any{"workflows": rows}
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/v1/workflows/"):
		name, decodeErr := url.PathUnescape(strings.TrimPrefix(path, "/v1/workflows/"))
		if decodeErr != nil {
			fail(w, 400, "invalid_path", "invalid path")
			return true
		}
		var record workflow.Record
		record, err = s.Workflows.Get(ctx, name)
		result = map[string]any{"workflow": record}
	case r.Method == http.MethodPost && (path == "/v1/workflows" || path == "/v1/workflows/check"):
		if path == "/v1/workflows" && !s.WorkflowWrites {
			fail(w, 501, "read_only", "workflow mutations require writable Go Control mode")
			return true
		}
		decode := func(value any) bool {
			d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
			d.UseNumber()
			if err := d.Decode(value); err != nil {
				fail(w, 400, "invalid_body", "expected a bounded JSON workflow request")
				return false
			}
			var extra any
			if d.Decode(&extra) != io.EOF {
				fail(w, 400, "invalid_body", "expected one JSON workflow request")
				return false
			}
			return true
		}
		if path == "/v1/workflows/check" {
			var request workflow.CheckRequest
			if !decode(&request) {
				return true
			}
			result, err = s.Workflows.Check(ctx, request)
		} else {
			var request workflow.RecordRequest
			if !decode(&request) {
				return true
			}
			var record workflow.Record
			record, err = s.Workflows.Record(ctx, token, request)
			result = map[string]any{"workflow": record}
		}
	default:
		return false
	}
	if err != nil {
		respondError(w, err)
	} else {
		write(w, 200, result)
	}
	return true
}
