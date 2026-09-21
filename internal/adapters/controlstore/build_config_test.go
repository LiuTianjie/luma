package controlstore

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/deployment"
)

func TestBuildConfigAtomicMergeAliasesAndDirectEgress(t *testing.T) {
	s, _, _ := buildFixture(t)
	ctx := context.Background()
	if _, err := s.writer.Exec(`UPDATE control_entities SET payload=json_set(payload,'$.displayName','Alias','$.region','cn','$.agent.os','linux','$.agent.arch','arm64') WHERE kind='nodes' AND id='worker'`); err != nil {
		t.Fatal(err)
	}
	for key, value := range map[string]string{
		"build":       `{"extension":{"keep":true},"registryHost":"old.test","pushHost":"old.test"}`,
		"setupChecks": `{"checkedAt":123,"checks":{"registry":{"status":"ready"},"cloudflare":{"status":"ready"}}}`,
	} {
		if _, err := s.writer.Exec(`INSERT OR REPLACE INTO control_config(key,payload) VALUES(?,?)`, key, value); err != nil {
			t.Fatal(err)
		}
	}
	pull, push := "REGISTRY.test:5000", "100.64.0.8:5000"
	direct := []string{"Alias", "worker"}
	result, err := s.ChangeBuildConfig(ctx, "management", builddomain.ConfigChange{Nodes: []string{"Alias"}, RegistryHost: &pull, PushHost: &push, DirectEgressNodes: &direct})
	if err != nil {
		t.Fatal(err)
	}
	if result.DefaultNode != "worker" || result.RegistryHost != "registry.test:5000" || len(result.Nodes) != 1 || !result.Nodes[0].Ready || len(result.DirectEgressNodes) != 1 || result.DirectEgressNodes[0] != "worker" {
		t.Fatal(result)
	}
	plan, err := s.PlanBuild(ctx, builddomain.ControlRequest{RepoURL: "team/app"}, deployment.RenderConfig{})
	if err != nil || plan.Request.BuildNode != "worker" || plan.Image.RegistryHost != "registry.test:5000" || plan.Image.PushHost != push || plan.Image.Proxy != "" {
		t.Fatal(plan, err)
	}
	var raw string
	if err := s.writer.QueryRow(`SELECT payload FROM control_config WHERE key='build'`).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(raw, `"extension":{"keep":true}`) {
		t.Fatal(raw)
	}
	before := raw
	unknown := []string{"missing"}
	for _, change := range []builddomain.ConfigChange{{DefaultNode: "missing"}, {Nodes: []string{"worker", "missing"}}, {RegistryHost: &push, DirectEgressNodes: &unknown}} {
		if _, err := s.ChangeBuildConfig(ctx, "management", change); !errors.Is(err, builddomain.ErrInput) {
			t.Fatal(err)
		}
		if err := s.writer.QueryRow(`SELECT payload FROM control_config WHERE key='build'`).Scan(&raw); err != nil || raw != before {
			t.Fatal(raw, err)
		}
	}
	if _, err := s.ChangeBuildConfig(ctx, "wrong", builddomain.ConfigChange{DefaultNode: "worker"}); !errors.Is(err, credentials.ErrUnauthorized) {
		t.Fatal(err)
	}
	if err := s.writer.QueryRow(`SELECT payload FROM control_config WHERE key='setupChecks'`).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var checks map[string]any
	_ = json.Unmarshal([]byte(raw), &checks)
	if checks["checkedAt"] != float64(123) || strings.Contains(raw, `"registry":{"status":"ready"}`) || !strings.Contains(raw, `"cloudflare":{"status":"ready"}`) {
		t.Fatal(raw)
	}
	cleared := []string{}
	result, err = s.ChangeBuildConfig(ctx, "management", builddomain.ConfigChange{DirectEgressNodes: &cleared})
	if err != nil || len(result.DirectEgressNodes) != 0 {
		t.Fatal(result, err)
	}
	plan, err = s.PlanBuild(ctx, builddomain.ControlRequest{RepoURL: "team/app"}, deployment.RenderConfig{NomadServer: "100.64.0.1"})
	if err != nil || plan.Image.Proxy != "http://100.64.0.1:7890" {
		t.Fatal(plan, err)
	}
	// A per-request direct override still wins after the default list is cleared.
	plan, err = s.PlanBuild(ctx, builddomain.ControlRequest{RepoURL: "team/app", ProxyMode: "direct"}, deployment.RenderConfig{})
	if err != nil || plan.Image.Proxy != "" {
		t.Fatal(plan, err)
	}
}
func TestBuildConfigDoesNotBuildOnUndeclaredNodes(t *testing.T) {
	s, _, _ := buildFixture(t)
	if _, err := s.writer.Exec(`UPDATE control_entities SET payload=json_set(payload,'$.region','global','$.agent.os','linux','$.agent.arch','arm64') WHERE kind='nodes' AND id='worker'`); err != nil {
		t.Fatal(err)
	}
	request := builddomain.ControlRequest{RepoURL: "team/app", BuildNode: "worker", RegistryHost: "registry.test:5000"}
	if _, err := s.PlanBuild(context.Background(), request, deployment.RenderConfig{}); !errors.Is(err, builddomain.ErrInput) {
		t.Fatal("undeclared builder was accepted", err)
	}
	if _, err := s.ChangeBuildConfig(context.Background(), "management", builddomain.ConfigChange{Nodes: []string{"worker"}}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.PlanBuild(context.Background(), request, deployment.RenderConfig{}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.writer.Exec(`UPDATE control_entities SET payload=json_set(payload,'$.drain',json('true')) WHERE kind='nodes' AND id='worker'`); err != nil {
		t.Fatal(err)
	}
	summary, err := s.BuildConfig(context.Background())
	if err != nil || len(summary.Nodes) != 1 || summary.Nodes[0].Ready {
		t.Fatal(summary, err)
	}
	if _, err := s.PlanBuild(context.Background(), request, deployment.RenderConfig{}); !errors.Is(err, builddomain.ErrInput) {
		t.Fatal("drained builder was accepted", err)
	}
}

func TestBuildConfigSummaryReadinessAndNoCredentials(t *testing.T) {
	s, _, _ := buildFixture(t)
	ctx := context.Background()
	if _, err := s.writer.Exec(`INSERT OR REPLACE INTO control_config(key,payload) VALUES('build','{"defaultNode":"worker","nodes":["worker"],"extensionSecret":"private-config"}')`); err != nil {
		t.Fatal(err)
	}
	for _, scenario := range []struct {
		status  string
		actions string
		ready   bool
		want    string
	}{
		{"online", `["build-image"]`, true, "ready"},
		{"online", `[]`, false, "ready"},
		{"offline", `["build-image"]`, false, "offline"},
	} {
		if _, err := s.writer.Exec(`UPDATE control_entities SET payload=json_set(payload,'$.agent.status',?,'$.agent.supportedActions',json(?),'$.agent.privateCredential','never-public') WHERE kind='nodes' AND id='worker'`, scenario.status, scenario.actions); err != nil {
			t.Fatal(err)
		}
		result, err := s.BuildConfig(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if len(result.Nodes) != 1 || result.Nodes[0].Ready != scenario.ready || result.Nodes[0].AgentStatus != scenario.want {
			t.Fatal(result)
		}
		raw, _ := json.Marshal(result)
		if strings.Contains(string(raw), "private-config") || strings.Contains(string(raw), "never-public") {
			t.Fatal(string(raw))
		}
	}
	if _, err := s.writer.Exec(`INSERT INTO control_entities(kind,id,payload) VALUES('nodes','other','{"name":"other","displayName":"shared"}'); UPDATE control_entities SET payload=json_set(payload,'$.displayName','shared') WHERE kind='nodes' AND id='worker'`); err != nil {
		t.Fatal(err)
	}
	if _, err := s.ChangeBuildConfig(ctx, "management", builddomain.ConfigChange{DefaultNode: "shared"}); !errors.Is(err, builddomain.ErrInput) {
		t.Fatal(err)
	}
}
