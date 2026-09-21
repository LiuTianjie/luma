package controlserver

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/deployment"
)

func deploymentPath(path string) bool {
	switch path {
	case "/v1/deployments", "/v1/deployments/stream", "/v1/deployments/preview", "/v1/compose-deployments", "/v1/compose-deployments/stream", "/v1/compose-deployments/preview":
		return true
	}
	return false
}
func (s Server) deploymentRequest(ctx context.Context, w http.ResponseWriter, r *http.Request, token string) bool {
	if r.Method != "POST" || !deploymentPath(r.URL.Path) {
		return false
	}
	var request deployment.ControlRequest
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&request) != nil {
		fail(w, 400, "invalid_body", "expected a bounded deployment request containing supported fields")
		return true
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		fail(w, 400, "invalid_body", "expected one deployment request")
		return true
	}
	compose := strings.HasPrefix(r.URL.Path, "/v1/compose-")
	if strings.HasSuffix(r.URL.Path, "/preview") {
		prepared, err := s.Deployments.Prepare(ctx, request, compose)
		if err != nil {
			respondError(w, err)
			return true
		}
		reports, err := s.Deployments.RequirementReports(ctx, request, prepared, nil)
		if err != nil {
			respondError(w, err)
			return true
		}
		write(w, 200, map[string]any{"clusterId": prepared.Snapshot.ClusterID, "service": prepared.Record.Name, "sourceName": prepared.Record.SourceName, "artifacts": prepared.Plan.Artifacts, "warnings": prepared.Plan.Warnings, "summary": map[string]any{"name": prepared.Record.Name, "node": prepared.Record.Node, "nodeId": prepared.Record.NodeID, "region": prepared.Plan.Region}, "localStorage": prepared.Record.LocalStorage, "dnsRecords": prepared.DNSRecords, "requirements": prepared.PublicRequirements(reports)})
		return true
	}
	if !strings.HasSuffix(r.URL.Path, "/stream") {
		if deadline, ok := ctx.Deadline(); ok {
			_ = http.NewResponseController(w).SetWriteDeadline(deadline.Add(5 * time.Second))
		}
		result, err := s.Deployments.Deploy(ctx, token, request, compose, nil)
		if err != nil {
			respondError(w, err)
		} else {
			write(w, 200, result)
		}
		return true
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	type finished struct {
		result map[string]any
		err    error
	}
	done := make(chan finished, 1)
	events := make(chan deployment.Event, 8)
	go func() {
		result, err := s.Deployments.Deploy(ctx, token, request, compose, func(e deployment.Event) error {
			select {
			case events <- e:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		})
		done <- finished{result, err}
	}()
	w.Header().Set("Content-Type", "application/x-ndjson")
	rc := http.NewResponseController(w)
	send := func(value any) error {
		_ = rc.SetWriteDeadline(time.Now().Add(30 * time.Second))
		if err := json.NewEncoder(w).Encode(value); err != nil {
			return err
		}
		return rc.Flush()
	}
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		var err error
		select {
		case event := <-events:
			err = send(map[string]any{"status": event.Status, "message": event.Message})
		case result := <-done:
			if result.err != nil {
				_ = send(map[string]any{"status": "fail", "message": result.err.Error()})
			} else {
				_ = send(map[string]any{"status": "done", "result": result.result})
			}
			return true
		case <-ticker.C:
			err = send(map[string]string{"status": "waiting", "message": "Waiting for this Nomad rollout"})
		case <-ctx.Done():
			cancel()
			<-done
			return true
		}
		if err != nil {
			cancel()
			<-done
			return true
		}
	}
}
