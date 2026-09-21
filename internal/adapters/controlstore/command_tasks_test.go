package controlstore

import (
	"context"
	"encoding/json"
	"errors"
	"testing"

	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/node"
)

func TestDeploymentOwnsCacheTasksAndCredentialsStayOutOfQueue(t *testing.T) {
	dir, db := fixture(t)
	ctx := context.Background()
	for _, statement := range []string{
		`CREATE TABLE control_collections(kind TEXT PRIMARY KEY,shape TEXT)`,
		`INSERT INTO control_entities(kind,id,payload) VALUES('nodes','builder','{"agent":{"status":"ready","tokenHash":"fixture","capabilities":["runtime-image-cache-v1"],"supportedActions":["cache-runtime-image"]}}')`,
	} {
		if _, err := db.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	s, err := OpenWriter(ctx, dir)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	snapshot, err := s.DeploymentSnapshot(ctx, "service", "app")
	if err != nil {
		t.Fatal(err)
	}
	r := deployment.Record{Kind: "service", Slug: "app", OperationID: "deploy-fixture", Status: "pending"}
	if err := s.BeginDeployment(ctx, "management", snapshot.Revision, r, nil); err != nil {
		t.Fatal(err)
	}
	owner := node.TaskOwner{Kind: r.Kind, Slug: r.Slug, OperationID: r.OperationID}
	task := node.StoredTask{ID: "cache-1", NodeName: "builder", Action: "cache-runtime-image", RequiredCapability: "runtime-image-cache-v1", Status: "queued", Payload: json.RawMessage(`{"sourceImage":"source.test/app:1","pushImage":"registry.test/luma-cache/app:1","destinationImage":"registry.test/luma-cache/app:1"}`)}
	withSecret := task
	withSecret.Payload = json.RawMessage(`{"sourceRegistryAuth":{"password":"must-not-persist"}}`)
	if err := s.CreateCommandTask(ctx, "management", owner, withSecret); !errors.Is(err, node.ErrControlInput) {
		t.Fatal(err)
	}
	if err := s.CreateCommandTask(ctx, "management", owner, task); err != nil {
		t.Fatal(err)
	}
	other := owner
	other.OperationID = "deploy-other"
	if _, err := s.CommandTask(ctx, other, task.ID); !errors.Is(err, node.ErrTaskNotFound) {
		t.Fatal(err)
	}
	if err := s.CancelCommandTask(ctx, other, task.ID); !errors.Is(err, node.ErrTaskNotFound) {
		t.Fatal(err)
	}
	if _, err := db.Exec(`UPDATE control_config SET payload='"rotated"' WHERE key='deployToken'`); err != nil {
		t.Fatal(err)
	}
	task.ID = "cache-2"
	if err := s.CreateCommandTask(ctx, "management", owner, task); !errors.Is(err, credentials.ErrUnauthorized) {
		t.Fatal(err)
	}
	if err := s.CreateCommandTask(ctx, "rotated", owner, task); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`UPDATE control_entities SET status='running',payload=json_set(payload,'$.status','running') WHERE kind='agentTasks' AND id='cache-2'`); err != nil {
		t.Fatal(err)
	}
	// A deployment ending cancels its queued work and asks a running Agent to stop.
	r.Status = "failed"
	if err := s.FinishDeployment(ctx, r); err != nil {
		t.Fatal(err)
	}
	first, err := s.CommandTask(ctx, owner, "cache-1")
	if err != nil || first.Status != "canceled" {
		t.Fatal(first, err)
	}
	second, err := s.CommandTask(ctx, owner, "cache-2")
	if err != nil || second.Status != "running" || second.CancelRequestedAt == 0 {
		t.Fatal(second, err)
	}
	task.ID = "cache-after-end"
	if err := s.CreateCommandTask(ctx, "rotated", owner, task); !errors.Is(err, deployment.ErrConflict) {
		t.Fatal(err)
	}
}
