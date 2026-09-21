package deployment_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/LiuTianjie/luma/internal/deployment"
)

type compiler struct{}

func (compiler) Compile(deployment.Service, deployment.RenderConfig) (deployment.Plan, error) {
	return deployment.Plan{}, nil
}

type gateway struct {
	events           []deployment.Event
	err              error
	submits, records int
	status           string
	recordErr        error
}

func (g *gateway) Submit(_ context.Context, _ deployment.SubmitRequest, emit func(deployment.Event) error) error {
	g.submits++
	for _, e := range g.events {
		if err := emit(e); err != nil {
			return err
		}
	}
	return g.err
}
func (g *gateway) Check(context.Context, deployment.Workflow) (deployment.WorkflowCheck, error) {
	return deployment.WorkflowCheck{Status: g.status, Revision: 7}, nil
}
func (g *gateway) Record(_ context.Context, w deployment.Workflow, _ []byte) error {
	g.records++
	if w.ExpectedRevision != 7 {
		panic("revision lost")
	}
	return g.recordErr
}

func TestDeploymentRequiresTerminalSuccessAndNeverReplays(t *testing.T) {
	for _, test := range []struct {
		name    string
		events  []deployment.Event
		err     error
		wantErr bool
	}{
		{"success", []deployment.Event{{Status: "start"}, {Status: "done", Result: []byte(`{"service":"api"}`)}}, nil, false},
		{"empty", nil, nil, true},
		{"progress then EOF", []deployment.Event{{Status: "ok"}}, nil, true},
		{"failed", []deployment.Event{{Status: "fail", Message: "rollout failed"}}, nil, true},
		{"timeout", nil, context.DeadlineExceeded, true},
		{"missing result", []deployment.Event{{Status: "done"}}, nil, true},
		{"duplicate completion", []deployment.Event{{Status: "done", Result: []byte(`{}`)}, {Status: "done", Result: []byte(`{}`)}}, nil, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			g := &gateway{events: test.events, err: test.err, status: "match"}
			app := deployment.Application{Compiler: compiler{}, Submitter: g, Workflows: g}
			_, err := app.Deploy(context.Background(), deployment.Service{Name: "api", Image: "image", Region: "cn", Exposure: "none", Replicas: 1}, deployment.RenderConfig{}, deployment.SubmitRequest{}, nil)
			if (err != nil) != test.wantErr {
				t.Fatalf("error=%v", err)
			}
			if g.submits != 1 {
				t.Fatalf("submits=%d", g.submits)
			}
			if test.wantErr && g.records != 0 {
				t.Fatal("failed deployment recorded")
			}
		})
	}
}
func TestWorkflowGuardAndRecordFailure(t *testing.T) {
	g := &gateway{status: "confirmation-required", events: []deployment.Event{{Status: "done", Result: []byte(`{}`)}}, recordErr: errors.New("revision conflict")}
	app := deployment.Application{Compiler: compiler{}, Submitter: g, Workflows: g}
	s := deployment.Service{Name: "api", Image: "image", Region: "cn", Exposure: "none", Replicas: 1}
	_, err := app.Deploy(context.Background(), s, deployment.RenderConfig{}, deployment.SubmitRequest{}, nil)
	if err == nil || g.submits != 0 {
		t.Fatal("workflow guard bypassed")
	}
	warning := ""
	result, err := app.Deploy(context.Background(), s, deployment.RenderConfig{}, deployment.SubmitRequest{AcceptWorkflowChange: true}, func(e deployment.Event) error {
		if e.Status == "warn" {
			warning = e.Message
		}
		return nil
	})
	if err != nil || len(result) == 0 || !strings.Contains(warning, "revision conflict") {
		t.Fatalf("result=%s error=%v warning=%s", result, err, warning)
	}
}
