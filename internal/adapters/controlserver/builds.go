package controlserver

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/LiuTianjie/luma/internal/history"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	builddomain "github.com/LiuTianjie/luma/internal/build"
)

func (s Server) buildRequest(ctx context.Context, w http.ResponseWriter, r *http.Request, token string) bool {
	if r.Method != "POST" {
		return false
	}
	if strings.HasPrefix(r.URL.Path, "/v1/builds/") && strings.HasSuffix(r.URL.Path, "/cancel") {
		id := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/v1/builds/"), "/cancel")
		if id == "" || strings.Contains(id, "/") {
			return false
		}
		var body map[string]any
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
		if err := decoder.Decode(&body); err != nil && err != io.EOF || len(body) != 0 {
			fail(w, 400, "invalid_body", "build cancel request body must be empty")
			return true
		}
		var extra any
		if decoder.Decode(&extra) != io.EOF {
			fail(w, 400, "invalid_body", "expected one cancel request")
			return true
		}
		previous, err := s.Builds.Repository.ReadBuild(ctx, id)
		if err != nil {
			respondError(w, err)
			return true
		}
		result, err := s.Builds.Repository.CancelBuild(ctx, token, id)
		if err != nil {
			respondError(w, err)
		} else {
			write(w, 200, map[string]any{"run": result, "replayed": builddomain.Terminal(previous.Status)})
		}
		return true
	}
	retryID := buildRetryID(r.URL.Path)
	update := r.URL.Path == "/v1/applications/update" || r.URL.Path == "/v1/applications/update/stream"
	if r.URL.Path != "/v1/builds" && r.URL.Path != "/v1/builds/stream" && retryID == "" && !update {
		return false
	}
	decode := func(value any) bool {
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20))
		decoder.DisallowUnknownFields()
		if decoder.Decode(value) != nil {
			fail(w, 400, "invalid_body", "expected a bounded build request containing supported fields")
			return false
		}
		var extra any
		if decoder.Decode(&extra) != io.EOF {
			fail(w, 400, "invalid_body", "expected one build request")
			return false
		}
		return true
	}
	var run builddomain.Run
	var err error
	queued := false
	switch {
	case retryID != "":
		var request builddomain.RetryRequest
		if !decode(&request) {
			return true
		}
		queued = request.Queue
		run, err = s.Builds.Retry(ctx, token, retryID, request)
	case update:
		var request builddomain.UpdateRequest
		if !decode(&request) {
			return true
		}
		queued = request.Queue
		run, err = s.Builds.Update(ctx, token, request)
	default:
		var request builddomain.ControlRequest
		if !decode(&request) {
			return true
		}
		queued = request.Queue
		run, err = s.Builds.Submit(ctx, token, request)
	}
	if err != nil {
		respondError(w, err)
		return true
	}
	return s.buildResponse(ctx, w, r, run, queued)
}

func (s Server) buildResponse(ctx context.Context, w http.ResponseWriter, r *http.Request, run builddomain.Run, queued bool) bool {
	stream := strings.HasSuffix(r.URL.Path, "/stream")
	if queued {
		accepted := map[string]any{"queued": true, "buildRunId": run.ID, "run": run}
		if stream {
			w.Header().Set("Content-Type", "application/x-ndjson")
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "done", "result": accepted})
		} else {
			write(w, 200, accepted)
		}
		return true
	}
	rc := http.NewResponseController(w)
	if stream {
		w.Header().Set("Content-Type", "application/x-ndjson")
	} else if deadline, ok := ctx.Deadline(); ok {
		_ = rc.SetWriteDeadline(deadline.Add(5 * time.Second))
	}
	send := func(value any) error {
		_ = rc.SetWriteDeadline(time.Now().Add(30 * time.Second))
		if err := json.NewEncoder(w).Encode(value); err != nil {
			return err
		}
		return rc.Flush()
	}
	if stream {
		if send(map[string]any{"status": "start", "message": "Build attempt created", "buildRunId": run.ID}) != nil {
			return true
		}
	}
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	heartbeat := time.NewTicker(15 * time.Second)
	defer heartbeat.Stop()
	previous := ""
	after := int64(-1)
	for {
		current, events, next, more, err := s.readBuildResponse(ctx, run.ID, stream, after)
		if err != nil {
			if stream {
				_ = send(map[string]any{"status": "fail", "message": "Build status unavailable", "buildRunId": run.ID})
			} else {
				respondError(w, err)
			}
			return true
		}
		for _, event := range events {
			event["buildRunId"] = run.ID
			if send(event) != nil {
				return true
			}
		}
		after = next
		if more {
			continue
		}
		if builddomain.Terminal(current.Status) {
			if current.Status == "succeeded" {
				if stream {
					_ = send(map[string]any{"status": "done", "result": current.Result})
				} else {
					write(w, 200, current.Result)
				}
			} else {
				if stream {
					_ = send(map[string]any{"status": "fail", "message": current.Message, "buildRunId": run.ID})
				} else {
					write(w, 409, map[string]any{"error": current.Message, "buildRunId": run.ID, "run": current})
				}
			}
			return true
		}
		if stream && previous != current.Status+current.Message {
			if send(map[string]any{"status": "waiting", "message": current.Message, "buildRunId": run.ID}) != nil {
				return true
			}
			previous = current.Status + current.Message
		}
		select {
		case <-ctx.Done():
			return true
		case <-ticker.C:
		case <-heartbeat.C:
			if stream {
				if send(map[string]any{"status": "waiting", "message": "Waiting for build and deployment", "buildRunId": run.ID}) != nil {
					return true
				}
			}
		}
	}
}

func buildRetryID(path string) string {
	if !strings.HasPrefix(path, "/v1/builds/") {
		return ""
	}
	value := strings.TrimPrefix(path, "/v1/builds/")
	if strings.HasSuffix(value, "/stream") {
		value = strings.TrimSuffix(value, "/stream")
	}
	if !strings.HasSuffix(value, "/retry") {
		return ""
	}
	id := strings.TrimSuffix(value, "/retry")
	if id == "" || strings.Contains(id, "/") {
		return ""
	}
	return id
}
func buildMutationPath(path string) bool {
	return strings.HasPrefix(path, "/v1/builds/local/") || path == "/v1/builds" || path == "/v1/builds/stream" || buildRetryID(path) != "" || path == "/v1/applications/update" || path == "/v1/applications/update/stream"
}
func (s Server) readBuildResponse(ctx context.Context, id string, stream bool, after int64) (builddomain.Run, []map[string]any, int64, bool, error) {
	if !stream {
		r, err := s.Builds.Repository.ReadBuild(ctx, id)
		return r, nil, after, false, err
	}
	detail, err := s.History.Detail(ctx, "build", id, map[string]string{"after": strconv.FormatInt(after, 10), "limit": "100"}, true)
	if err != nil {
		return builddomain.Run{}, nil, after, false, err
	}
	raw, err := json.Marshal(detail["run"])
	if err != nil {
		return builddomain.Run{}, nil, after, false, err
	}
	var r builddomain.Run
	if err := json.Unmarshal(raw, &r); err != nil {
		return r, nil, after, false, err
	}
	page, ok := detail["eventsPage"].(history.Page)
	if !ok || page.ResumeAfter == nil {
		return r, nil, after, false, fmt.Errorf("invalid build event page")
	}
	record, _ := detail["run"].(map[string]any)
	events, _ := record["events"].([]map[string]any)
	return r, events, *page.ResumeAfter, page.HasMore, nil
}
