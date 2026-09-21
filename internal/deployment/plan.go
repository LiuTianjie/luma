package deployment

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/LiuTianjie/luma/internal/workflow"
)

type Artifact struct {
	Kind    string `json:"kind"`
	Path    string `json:"path"`
	Content string `json:"content"`
}
type Plan struct {
	Name      string     `json:"name"`
	Region    string     `json:"region"`
	Exposure  string     `json:"exposure"`
	Kind      string     `json:"serviceKind"`
	Artifacts []Artifact `json:"artifacts"`
	Warnings  []string   `json:"warnings,omitempty"`
}

// Compiler consumes explicit inputs so Control and CLI can use the same policy.
type Compiler interface {
	Compile(Service, RenderConfig) (Plan, error)
}

// Event keeps the wire payload intact; the transport adapter owns JSON decoding.
type Event struct {
	Status, Message string
	Result, Wire    []byte
}
type SubmitRequest struct {
	Manifest, SourceName string
	ComposeContent       string
	SkipDNS              bool
	EnvSecrets           map[string]string
	Workflow             Workflow
	AcceptWorkflowChange bool
}
type Submitter interface {
	Submit(context.Context, SubmitRequest, func(Event) error) error
}

type Application struct {
	Compiler        Compiler
	ComposeCompiler ComposeCompiler
	Submitter       Submitter
	Workflows       WorkflowStore
}

func (a Application) Preview(s Service, config RenderConfig) (Plan, error) {
	if err := s.Validate(); err != nil {
		return Plan{}, err
	}
	if config.Engine != "" && config.Engine != "nomad" && s.Engine == "" {
		return Plan{}, fmt.Errorf("engine must be nomad")
	}
	return a.Compiler.Compile(s, config)
}

// Deploy submits exactly once. EOF, timeout or cancellation is not proof that
// Control rejected the request; none of those conditions permits a replay.
func (a Application) Deploy(ctx context.Context, s Service, config RenderConfig, request SubmitRequest, emit func(Event) error) ([]byte, error) {
	if _, err := a.Preview(s, config); err != nil {
		return nil, err
	}
	return a.submit(ctx, request, emit)
}

func (a Application) submit(ctx context.Context, request SubmitRequest, emit func(Event) error) ([]byte, error) {
	if a.Submitter == nil {
		return nil, fmt.Errorf("deployment submitter is not configured")
	}
	return ExecuteWorkflow(ctx, a.Workflows, request.Workflow, request.AcceptWorkflowChange, emit, func(checked Workflow) ([]byte, error) {
		request.Workflow = checked
		var result []byte
		err := a.Submitter.Submit(ctx, request, func(event Event) error {
			if result != nil {
				return fmt.Errorf("control sent events after deployment completion")
			}
			if emit != nil {
				if err := emit(event); err != nil {
					return err
				}
			}
			switch event.Status {
			case "fail":
				return fmt.Errorf("deployment failed: %s", event.Message)
			case "done":
				if len(event.Result) == 0 {
					return fmt.Errorf("control stream ended without a deploy result")
				}
				result = append([]byte(nil), event.Result...)
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
		if result == nil {
			return nil, fmt.Errorf("control stream ended without a deploy result; deployment may still be running, inspect its status before retrying")
		}
		return result, nil
	})
}

// ExecuteWorkflow checks before accepting work and records only an actual
// successful result. A failed record must never cause duplicate deployment.
func ExecuteWorkflow(ctx context.Context, store WorkflowStore, w Workflow, accept bool, emit func(Event) error, operation func(Workflow) ([]byte, error)) ([]byte, error) {
	if store == nil {
		return nil, fmt.Errorf("workflow store is not configured")
	}
	checked, err := store.Check(ctx, w)
	if err != nil {
		return nil, err
	}
	switch checked.Status {
	case "unrecorded", "match":
	case "confirmation-required":
		if !accept {
			return nil, fmt.Errorf("deployment workflow differs: %s; deployment was not started; review and rerun with --accept-workflow-change", checked.Differences)
		}
	default:
		return nil, fmt.Errorf("Control returned an invalid workflow check; deployment was not started")
	}
	w.ExpectedRevision = checked.Revision
	result, err := operation(w)
	if err != nil {
		return nil, err
	}
	// Repository-only checks may resolve an existing sibling application's
	// recipe before the build discovers this application's actual name. Create
	// the new target at revision zero; never apply the sibling's revision to it.
	if w.Selector.Name == "" && checked.Name != "" {
		var target struct {
			Service    string `json:"service"`
			Deployment string `json:"deployment"`
		}
		if json.Unmarshal(result, &target) == nil {
			name := target.Service
			if name == "" {
				name = target.Deployment
			}
			if name != "" && workflow.Slug(name) != workflow.Slug(checked.Name) {
				w.ExpectedRevision = 0
			}
		}
	}
	if err := store.Record(ctx, w, result); err != nil && emit != nil {
		// The deployment already succeeded. A recording failure must not invite
		// a duplicate deployment by turning success into an ambiguous CLI failure.
		_ = emit(Event{Status: "warn", Message: "deployment succeeded, but workflow recording failed: " + err.Error()})
	}
	return result, nil
}
