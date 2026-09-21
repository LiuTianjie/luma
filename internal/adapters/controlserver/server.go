// Package controlserver owns HTTP translation; policies and repositories remain
// independent of request routing and response serialization.
package controlserver

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/history"
	"github.com/LiuTianjie/luma/internal/node"
	"github.com/LiuTianjie/luma/internal/registry"
	"github.com/LiuTianjie/luma/internal/storage"
	"github.com/LiuTianjie/luma/internal/workflow"
)

type Server struct {
	BuildConfig       builddomain.ConfigRepository
	BuildConfigWrites bool
	Builds            *builddomain.Coordinator
	Registries        registry.CredentialRepository
	RegistryWrites    bool
	Secrets           credentials.SecretRepository
	SecretWrites      bool
	Deployments       *deployment.Coordinator
	Identity          credentials.ManagementRepository
	History           history.Service
	Version           string
	Agents            *node.Coordinator
	Workflows         *workflow.Service
	WorkflowWrites    bool
}

func (s Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if r.Method == http.MethodGet && r.URL.Path == "/v1/health" {
		capabilities := []string{"history-read-v1"}
		if s.BuildConfig != nil {
			capabilities = append(capabilities, "build-config-read-v1")
			if s.BuildConfigWrites {
				capabilities = append(capabilities, "build-config-v1")
			}
		}
		if s.Builds != nil {
			capabilities = append(capabilities, "repository-build-v1", "local-build-v1", "build-queue-v1", "build-proxy-mode-v1", "repository-compose-sidecar-v1")
		}
		if s.Agents != nil {
			capabilities = append(capabilities, "node-agent-tasks-v1")
		}
		if s.Workflows != nil {
			capabilities = append(capabilities, "deployment-workflow-read-v1")
		}
		if s.WorkflowWrites {
			capabilities = append(capabilities, "deployment-workflow-v1")
		}
		if s.Secrets != nil {
			capabilities = append(capabilities, "secret-names-v1")
			if s.SecretWrites {
				capabilities = append(capabilities, "secrets-v1")
			}
		}
		if s.Deployments != nil {
			capabilities = append(capabilities, "internal-deployment-v1")
			if s.Deployments.Files != nil && s.Deployments.DNS != nil && s.Deployments.Prober != nil {
				capabilities = append(capabilities, "edge-deployment-v1")
			}
		}
		if s.Registries != nil {
			capabilities = append(capabilities, "registry-credentials-read-v1")
			if s.RegistryWrites {
				capabilities = append(capabilities, "registry-credentials-v1")
			}
		}
		write(w, 200, map[string]any{"ok": true, "version": s.Version, "nodeJoinModel": "region-first", "capabilities": capabilities, "readOnly": s.Agents == nil && !s.WorkflowWrites})
		return
	}
	timeout := 15 * time.Second
	if s.Builds != nil && buildMutationPath(r.URL.Path) && r.Method == "POST" {
		timeout = 25 * time.Hour
	}
	if s.Deployments != nil && deploymentPath(r.URL.Path) {
		timeout = s.Deployments.Timeout + 30*time.Second
		if timeout <= 30*time.Second {
			timeout = 630 * time.Second
		}
	}
	if r.URL.Path == "/v1/node-agent/lease" {
		timeout = 35 * time.Second
	}
	ctx, cancel := context.WithTimeout(r.Context(), timeout)
	defer cancel()
	header := r.Header.Get("Authorization")
	if !strings.HasPrefix(header, "Bearer ") || len(r.Header.Values("Authorization")) != 1 {
		fail(w, 401, "unauthorized", "missing bearer token")
		return
	}
	token := strings.TrimSpace(strings.TrimPrefix(header, "Bearer "))
	if s.Agents != nil && r.Method == http.MethodPost && strings.HasPrefix(r.URL.Path, "/v1/node-agent/") {
		s.agentRequest(ctx, w, r, token)
		return
	}
	session, err := credentials.Authenticate(ctx, s.Identity, token)
	if err != nil {
		respondError(w, err)
		return
	}
	if r.Method == http.MethodPost && r.URL.Path == "/v1/auth/login/verify" {
		write(w, 200, session)
		return
	}
	if s.Secrets != nil && s.secretRequest(ctx, w, r, token) {
		return
	}
	if s.Registries != nil && s.registryRequest(ctx, w, r, token) {
		return
	}
	if s.Workflows != nil && s.workflowRequest(ctx, w, r, token) {
		return
	}
	if s.BuildConfig != nil && s.buildConfigRequest(ctx, w, r, token) {
		return
	}
	if s.Builds != nil && s.localBuildRequest(ctx, w, r, token) {
		return
	}
	if s.Builds != nil && s.buildRequest(ctx, w, r, token) {
		return
	}
	if s.Deployments != nil && s.deploymentRequest(ctx, w, r, token) {
		return
	}
	if r.Method != http.MethodGet {
		fail(w, 501, "not_migrated", "this operation has not been migrated to Go Control")
		return
	}
	values := map[string]string{}
	query, err := url.ParseQuery(r.URL.RawQuery)
	if err != nil {
		fail(w, 400, "invalid_query", "invalid query string")
		return
	}
	for key, list := range query {
		if len(list) > 0 {
			values[key] = list[len(list)-1]
		}
	}
	parts := strings.Split(strings.TrimPrefix(r.URL.EscapedPath(), "/"), "/")
	for i, part := range parts {
		decoded, err := url.PathUnescape(part)
		if err != nil {
			fail(w, 400, "invalid_path", "invalid path")
			return
		}
		parts[i] = decoded
	}
	var result map[string]any
	switch {
	case r.URL.Path == "/v1/history":
		result, err = s.History.List(ctx, values, "")
	case r.URL.Path == "/v1/builds":
		result, err = s.History.List(ctx, values, "runs")
	case r.URL.Path == "/v1/deployments/history":
		result, err = s.History.List(ctx, values, "events")
	case len(parts) == 4 && parts[0] == "v1" && parts[1] == "history":
		result, err = s.History.Detail(ctx, parts[2], parts[3], values, false)
	case len(parts) == 4 && parts[0] == "v1" && parts[1] == "deployments" && parts[2] == "history":
		result, err = s.History.Detail(ctx, "deployment", parts[3], values, true)
	case len(parts) == 3 && parts[0] == "v1" && parts[1] == "builds":
		result, err = s.History.Detail(ctx, "build", parts[2], values, true)
	default:
		fail(w, 404, "not_found", "not found")
		return
	}
	if err != nil {
		respondError(w, err)
		return
	}
	write(w, 200, result)
}

func respondError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, builddomain.ErrInput):
		fail(w, 400, "luma_error", err.Error())
	case errors.Is(err, builddomain.ErrState):
		fail(w, 409, "build_conflict", err.Error())
	case errors.Is(err, builddomain.ErrNotFound):
		fail(w, 404, "not_found", err.Error())
	case errors.Is(err, registry.ErrCredentialInput):
		fail(w, 400, "invalid_registry", err.Error())
	case errors.Is(err, credentials.ErrSecretInput):
		fail(w, 400, "invalid_secret", err.Error())
	case errors.Is(err, credentials.ErrUnauthorized):
		fail(w, 401, "unauthorized", "unauthorized")
	case errors.Is(err, history.ErrInvalid):
		fail(w, 400, "luma_error", strings.TrimPrefix(err.Error(), history.ErrInvalid.Error()+": "))
	case errors.Is(err, history.ErrNotFound):
		fail(w, 404, "not_found", history.ErrNotFound.Error())
	case errors.Is(err, node.ErrControlInput):
		fail(w, 400, "luma_error", strings.TrimPrefix(err.Error(), node.ErrControlInput.Error()+": "))
	case errors.Is(err, node.ErrTaskNotFound):
		fail(w, 404, "not_found", node.ErrTaskNotFound.Error())
	case errors.Is(err, node.ErrTaskState):
		fail(w, 409, "task_conflict", node.ErrTaskState.Error())
	case errors.Is(err, node.ErrActionNotMigrated):
		fail(w, 501, "not_migrated", node.ErrActionNotMigrated.Error())
	case errors.Is(err, workflow.ErrInvalid):
		fail(w, 400, "luma_error", strings.TrimPrefix(err.Error(), workflow.ErrInvalid.Error()+": "))
	case errors.Is(err, workflow.ErrNotFound):
		fail(w, 404, "not_found", workflow.ErrNotFound.Error())
	case errors.Is(err, workflow.ErrConflict):
		fail(w, 409, "workflow_conflict", workflow.ErrConflict.Error())
	case errors.Is(err, deployment.ErrInput):
		fail(w, 400, "luma_error", err.Error())
	case errors.Is(err, deployment.ErrNotMigrated):
		fail(w, 501, "not_migrated", err.Error())
	case errors.Is(err, storage.ErrOwnership), errors.Is(err, deployment.ErrConflict), errors.Is(err, deployment.ErrSuperseded):
		fail(w, 409, "deployment_conflict", err.Error())
	case errors.Is(err, deployment.ErrRuntime):
		fail(w, 502, "runtime_error", err.Error())
	case errors.Is(err, context.DeadlineExceeded):
		fail(w, 503, "timeout", "Control read timed out")
	default:
		fail(w, 500, "state_unavailable", "Control state could not be read")
	}
}

func (s Server) agentRequest(ctx context.Context, w http.ResponseWriter, r *http.Request, token string) {
	decode := func(value any) bool {
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20))
		if err := decoder.Decode(value); err != nil {
			fail(w, 400, "invalid_body", "expected a bounded JSON agent request")
			return false
		}
		var extra any
		if err := decoder.Decode(&extra); err != io.EOF {
			fail(w, 400, "invalid_body", "expected one JSON agent request")
			return false
		}
		return true
	}
	var result any
	var err error
	switch r.URL.Path {
	case "/v1/node-agent/lease":
		var p node.AgentReport
		if !decode(&p) {
			return
		}
		result, err = s.Agents.Lease(ctx, token, p)
	case "/v1/node-agent/heartbeat":
		var p node.AgentReport
		if !decode(&p) {
			return
		}
		result, err = s.Agents.Heartbeat(ctx, token, p)
	case "/v1/node-agent/tasks/complete":
		var p node.Completion
		if !decode(&p) {
			return
		}
		result, err = s.Agents.Complete(ctx, token, p)
	case "/v1/node-agent/tasks/progress":
		var p node.ProgressRequest
		if !decode(&p) {
			return
		}
		result, err = s.Agents.Progress(ctx, token, p)
	default:
		fail(w, 404, "not_found", "not found")
		return
	}
	if err != nil {
		respondError(w, err)
		return
	}
	write(w, 200, result)
}
func fail(w http.ResponseWriter, status int, code, message string) {
	var id [6]byte
	_, _ = rand.Read(id[:])
	requestID := "req-" + hex.EncodeToString(id[:])
	write(w, status, map[string]any{"error": message, "requestId": requestID, "errorInfo": map[string]string{"code": code, "message": message, "requestId": requestID}})
}
func write(w http.ResponseWriter, status int, value any) {
	data, err := json.Marshal(value)
	if err != nil {
		http.Error(w, "response encoding failed", 500)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(append(data, '\n'))
}
