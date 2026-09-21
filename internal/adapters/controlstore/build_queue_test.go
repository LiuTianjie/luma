package controlstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/node"
)

func buildFixture(t *testing.T) (*Store, node.Coordinator, node.AgentReport) {
	t.Helper()
	s, c, p := agentFixture(t)
	if _, err := s.writer.Exec(`CREATE TABLE control_collections(kind TEXT PRIMARY KEY,shape TEXT)`); err != nil {
		t.Fatal(err)
	}
	if _, err := s.writer.Exec(`DELETE FROM control_entities WHERE kind='agentTasks'`); err != nil {
		t.Fatal(err)
	}
	caps, actions := []string{"docker-build"}, []string{"build-image"}
	p.Capabilities, p.SupportedActions = &caps, &actions
	c.Now = time.Now
	if _, err := c.Heartbeat(context.Background(), "node-token", p); err != nil {
		t.Fatal(err)
	}
	return s, c, p
}
func submitBuild(t *testing.T, s *Store, id, project, replacement string) builddomain.Run {
	t.Helper()
	r, err := s.EnqueueBuild(context.Background(), "management", builddomain.Submission{Run: builddomain.Run{ID: id, Source: "https://github.com/team/" + project, ProjectKey: project, BuildNode: "worker", ReplacementKey: replacement}, Request: builddomain.ControlRequest{RepoURL: "https://github.com/team/" + project, Queue: true, EnvSecrets: map[string]string{"PASSWORD": "private-env"}}})
	if err != nil {
		t.Fatal(err)
	}
	return r
}
func attachBuild(t *testing.T, s *Store, id string) string {
	t.Helper()
	task, err := s.AttachBuildTask(context.Background(), id, json.RawMessage(`{"repoUrl":"https://github.com/team/repo","registryHost":"registry.test:5000","pushHost":"localhost:5000","repo":"team/repo"}`))
	if err != nil {
		t.Fatal(err)
	}
	return task
}
func claimBuild(t *testing.T, s *Store, want string) {
	t.Helper()
	work, err := s.ClaimBuild(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if want == "" {
		if work != nil {
			t.Fatalf("unexpected claim %+v", work)
		}
		return
	}
	if work == nil || work.Run.ID != want {
		t.Fatalf("claim=%+v want=%s", work, want)
	}
}
func TestBuildQueueSerialProjectsAndPrivateRequest(t *testing.T) {
	s, _, _ := buildFixture(t)
	ctx := context.Background()
	r := submitBuild(t, s, "a", "one", "")
	submitBuild(t, s, "b", "one", "")
	submitBuild(t, s, "c", "two", "")
	raw, _ := json.Marshal(r)
	if strings.Contains(string(raw), "private-env") {
		t.Fatal("public secret")
	}
	if _, err := s.EnqueueBuild(ctx, "bad", builddomain.Submission{}); !errors.Is(err, credentials.ErrUnauthorized) {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	got := make(chan string, 12)
	for range 12 {
		wg.Go(func() {
			w, err := s.ClaimBuild(ctx)
			if err != nil {
				t.Error(err)
			}
			if w != nil {
				if w.Request.EnvSecrets["PASSWORD"] != "private-env" {
					t.Error("private request lost")
				}
				got <- w.Run.ID
			}
		})
	}
	wg.Wait()
	close(got)
	ids := map[string]bool{}
	for id := range got {
		if ids[id] {
			t.Fatal("duplicate claim")
		}
		ids[id] = true
	}
	if !ids["a"] || !ids["c"] || len(ids) != 2 {
		t.Fatal(ids)
	}
	if err := s.FinishBuild(ctx, "a", "failed", nil, "fixture"); err != nil {
		t.Fatal(err)
	}
	claimBuild(t, s, "b")
	var queue string
	if err := s.db.QueryRow(`SELECT payload FROM control_config WHERE key='buildQueue'`).Scan(&queue); err != nil {
		t.Fatal(err)
	}
	var entries map[string]any
	_ = json.Unmarshal([]byte(queue), &entries)
	if entries["a"] != nil {
		t.Fatal("finished private queue retained")
	}
}
func TestBuildReplacementWaitsForChildAndUsesLeasedCredentials(t *testing.T) {
	s, c, p := buildFixture(t)
	ctx := context.Background()
	for key, value := range map[string]string{"secrets": `{"GITHUB_TOKEN":"git-private"}`, "registries": `{"registry.test:5000":{"username":"robot","password":"registry-private"}}`} {
		if _, err := s.writer.Exec(`INSERT INTO control_config VALUES(?,?)`, key, value); err != nil {
			t.Fatal(err)
		}
	}
	submitBuild(t, s, "old", "one", "service:app")
	claimBuild(t, s, "old")
	id := attachBuild(t, s, "old")
	lease, err := c.Lease(ctx, "node-token", p)
	if err != nil || lease.Task == nil || lease.Task.ID != id {
		t.Fatal(lease, err)
	}
	payload := string(lease.Task.Payload)
	if !strings.Contains(payload, "git-private") || !strings.Contains(payload, "registry-private") {
		t.Fatal("lease missing credentials")
	}
	task, err := s.BuildTask(ctx, "old")
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(task)
	if strings.Contains(string(raw), "private") {
		t.Fatal("credentials persisted")
	}
	submitBuild(t, s, "new", "two", "service:app")
	if err := s.FinishBuild(ctx, "old", "succeeded", map[string]any{"must": "discard"}, ""); err != nil {
		t.Fatal(err)
	}
	old, _ := s.ReadBuild(ctx, "old")
	if old.Status != "canceled" || !old.QueueExecuting || old.Result != nil {
		t.Fatal(old)
	}
	claimBuild(t, s, "")
	p.ActiveTaskID = &id
	heartbeat, err := c.Heartbeat(ctx, "node-token", p)
	if err != nil || !heartbeat.CancelRequested {
		t.Fatal(heartbeat, err)
	}
	if _, err := c.Complete(ctx, "node-token", node.Completion{AgentReport: p, TaskID: id, Status: "canceled", Result: json.RawMessage(`{}`)}); err != nil {
		t.Fatal(err)
	}
	claimBuild(t, s, "new")
	old, _ = s.ReadBuild(ctx, "old")
	if old.QueueExecuting {
		t.Fatal("completed child still holds project")
	}
}
func TestBuildDeploymentRequiresSuccessfulOwnedTask(t *testing.T) {
	s, c, p := buildFixture(t)
	ctx := context.Background()
	submitBuild(t, s, "run", "one", "")
	claimBuild(t, s, "run")
	if err := s.BeginBuildDeployment(ctx, "run"); err == nil {
		t.Fatal("deployed without a child")
	}
	id := attachBuild(t, s, "run")
	if err := s.BeginBuildDeployment(ctx, "run"); err == nil {
		t.Fatal("deployed before build")
	}
	if _, err := c.Lease(ctx, "node-token", p); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Complete(ctx, "node-token", node.Completion{AgentReport: p, TaskID: id, Status: "succeeded", Result: json.RawMessage(`{"image":"registry.test:5000/team/repo:abc"}`)}); err != nil {
		t.Fatal(err)
	}
	if err := s.BeginBuildDeployment(ctx, "run"); err != nil {
		t.Fatal(err)
	}
	if err := s.FinishBuild(ctx, "run", "succeeded", map[string]any{"image": "first"}, "done"); err != nil {
		t.Fatal(err)
	}
	if err := s.FinishBuild(ctx, "run", "failed", nil, "late"); err != nil {
		t.Fatal(err)
	}
	r, _ := s.ReadBuild(ctx, "run")
	if r.Status != "succeeded" || r.Result["image"] != "first" || r.QueueExecuting {
		t.Fatal(r)
	}
}
func TestBuildTaskRejectsUnownedAndDurableCredentials(t *testing.T) {
	s, c, p := buildFixture(t)
	ctx := context.Background()
	submitBuild(t, s, "run", "one", "")
	claimBuild(t, s, "run")
	for _, extra := range []string{`"gitToken":""`, `"registryAuth":null`, `"unknown":"secret"`} {
		_, err := s.AttachBuildTask(ctx, "run", json.RawMessage(`{"repoUrl":"https://github.com/a/b","registryHost":"r.test","repo":"a/b",`+extra+`}`))
		if !errors.Is(err, builddomain.ErrInput) {
			t.Fatal(err)
		}
	}
	id := attachBuild(t, s, "run")
	if _, err := s.writer.Exec(`UPDATE control_entities SET payload=json_set(payload,'$.agentTaskId','foreign') WHERE kind='buildRuns' AND id='run'`); err != nil {
		t.Fatal(err)
	}
	leased, err := c.Lease(ctx, "node-token", p)
	if err != nil || leased.Task != nil {
		t.Fatal(leased, err)
	}
	if _, err := c.Complete(ctx, "node-token", node.Completion{AgentReport: p, TaskID: id, Status: "succeeded"}); !errors.Is(err, node.ErrActionNotMigrated) {
		t.Fatal(err)
	}
}
func TestBuildRestartRetainsQueuedAndCleansInterrupted(t *testing.T) {
	s, _, _ := buildFixture(t)
	ctx := context.Background()
	submitBuild(t, s, "a", "one", "")
	claimBuild(t, s, "a")
	attachBuild(t, s, "a")
	submitBuild(t, s, "b", "one", "")
	if err := s.RecoverBuilds(ctx); err != nil {
		t.Fatal(err)
	}
	r, _ := s.ReadBuild(ctx, "a")
	if r.Status != "failed" || r.QueueExecuting {
		t.Fatal(r)
	}
	claimBuild(t, s, "b")
	// A nil result clears an earlier transitional value; unknown metadata survives.
	if _, err := s.writer.Exec(`UPDATE control_entities SET payload=json_set(payload,'$.result',json('{"stale":true}'),'$.extension','keep') WHERE kind='buildRuns' AND id='b'`); err != nil {
		t.Fatal(err)
	}
	if err := s.FinishBuild(ctx, "b", "failed", nil, "failed"); err != nil {
		t.Fatal(err)
	}
	var raw string
	if err := s.db.QueryRow(`SELECT payload FROM control_entities WHERE kind='buildRuns' AND id='b'`).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(raw, "stale") || !strings.Contains(raw, "keep") {
		t.Fatal(raw)
	}
}
func TestBuildProviderLeaseScope(t *testing.T) {
	s, c, p := buildFixture(t)
	ctx := context.Background()
	if _, err := s.writer.Exec(`INSERT INTO control_config VALUES('gitProviders','{"git":{"type":"gitea","baseUrl":"https://git.test/prefix","token":"provider-private","username":"bot"}}')`); err != nil {
		t.Fatal(err)
	}
	submitBuild(t, s, "run", "one", "")
	claimBuild(t, s, "run")
	id, err := s.AttachBuildTask(ctx, "run", json.RawMessage(`{"repoUrl":"https://evil.test/team/repo","gitProviderId":"git","registryHost":"registry.test","repo":"team/repo"}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Lease(ctx, "node-token", p); err == nil {
		t.Fatal("credential crossed provider origin")
	}
	// Replacing the fixture payload within a transaction also proves a failed
	// credential lookup did not commit a running lease.
	if err := s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		_, err := tx.Exec(`UPDATE control_entities SET payload=json_set(payload,'$.payload.repoUrl','https://git.test/prefix/team/repo') WHERE kind='agentTasks' AND id=?`, id)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	leased, err := c.Lease(ctx, "node-token", p)
	if err != nil || leased.Task == nil || !strings.Contains(string(leased.Task.Payload), "provider-private") {
		t.Fatal(leased, err)
	}
}

func TestBuildPrivateQueuePreservesExplicitEmptySecrets(t *testing.T) {
	s, _, _ := buildFixture(t)
	ctx := context.Background()
	_, err := s.EnqueueBuild(ctx, "management", builddomain.Submission{Run: builddomain.Run{ID: "empty", Source: "https://github.com/t/r", ProjectKey: "t/r", BuildNode: "worker"}, Request: builddomain.ControlRequest{EnvSecrets: map[string]string{}}})
	if err != nil {
		t.Fatal(err)
	}
	work, err := s.ClaimBuild(ctx)
	if err != nil || work == nil || work.Request.EnvSecrets == nil {
		t.Fatal(work, err)
	}
}

func TestAgentBuildProgressIsRecordedUnderBuildOwner(t *testing.T) {
	s, c, p := buildFixture(t)
	ctx := context.Background()
	submitBuild(t, s, "run", "one", "")
	claimBuild(t, s, "run")
	id := attachBuild(t, s, "run")
	if _, err := c.Lease(ctx, "node-token", p); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Progress(ctx, "node-token", node.ProgressRequest{Identity: p.Identity, TaskID: id, Events: []node.Event{{Type: "output", Line: "compile step"}}}); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := s.db.QueryRow(`SELECT COUNT(*) FROM control_events WHERE kind='buildRuns' AND entity_id='run' AND json_extract(payload,'$.message')='compile step'`).Scan(&count); err != nil || count != 1 {
		t.Fatal(count, err)
	}
}
