package controlstore

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/node"
)

func agentFixture(t *testing.T) (*Store, node.Coordinator, node.AgentReport) {
	t.Helper()
	dir, db := fixture(t)
	if _, err := db.Exec(`CREATE TABLE control_event_streams(kind TEXT,entity_id TEXT,stream TEXT,PRIMARY KEY(kind,entity_id,stream))`); err != nil {
		t.Fatal(err)
	}
	record := map[string]any{"nodeId": "node-id", "agent": map[string]any{"tokenHash": credentials.AgentTokenHash("node-token")}}
	raw, _ := json.Marshal(record)
	if _, err := db.Exec(`INSERT INTO control_entities VALUES('nodes','worker',?,'','',0,0,0)`, string(raw)); err != nil {
		t.Fatal(err)
	}
	for i := range 3 {
		task := node.StoredTask{ID: fmt.Sprintf("task-%d", i), NodeName: "worker", Status: "queued", Action: "prepare-managed-volume-path", Payload: json.RawMessage(`{"root":"/tmp/data","relative":"test"}`), CreatedAt: 100, UpdatedAt: 100}
		raw, _ := json.Marshal(task)
		if _, err := db.Exec(`INSERT INTO control_entities VALUES('agentTasks',?,?,'queued','',100,100,0)`, task.ID, string(raw)); err != nil {
			t.Fatal(err)
		}
	}
	store, err := OpenWriter(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })
	coordinator := node.Coordinator{Repository: store, Version: "test", Now: func() time.Time { return time.Unix(1000, 0) }}
	capabilities := []string{"managed-volume-path"}
	actions := []string{"prepare-managed-volume-path"}
	idle := ""
	p := node.AgentReport{Identity: node.Identity{Name: "worker", ID: "node-id"}, Capabilities: &capabilities, SupportedActions: &actions, ActiveTaskID: &idle}
	return store, coordinator, p
}

func TestAgentClaimsAreSerialAndOutcomeCommitsAreImmutable(t *testing.T) {
	store, c, p := agentFixture(t)
	ctx := context.Background()
	var group sync.WaitGroup
	results := make(chan node.LeaseReply, 16)
	errs := make(chan error, 16)
	for range 16 {
		group.Go(func() {
			result, err := c.Lease(ctx, "node-token", p)
			if err != nil {
				errs <- err
			} else {
				results <- result
			}
		})
	}
	group.Wait()
	close(results)
	close(errs)
	for err := range errs {
		t.Error(err)
	}
	claimed := 0
	for result := range results {
		if result.Task != nil {
			claimed++
			if result.Task.ID != "task-0" {
				t.Fatal(result.Task.ID)
			}
		}
	}
	if claimed != 1 {
		t.Fatalf("claims=%d", claimed)
	}
	r := node.Completion{AgentReport: node.AgentReport{Identity: p.Identity}, TaskID: "task-0", Status: "succeeded", Result: json.RawMessage(`{"first":true}`)}
	if _, err := c.Complete(ctx, "node-token", r); err != nil {
		t.Fatal(err)
	}
	r.Status = "failed"
	r.Result = json.RawMessage(`{"later":true}`)
	result, err := c.Complete(ctx, "node-token", r)
	if err != nil || result.Status != "succeeded" {
		t.Fatal(result, err)
	}
	if err := store.AgentTransaction(ctx, "node-token", p.Identity, func(tx node.AgentTransaction) error {
		task, err := tx.Task("task-0")
		if err != nil {
			return err
		}
		if string(task.Result) != `{"first":true}` {
			t.Fatal(string(task.Result))
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if next, err := c.Lease(ctx, "node-token", p); err != nil || next.Task == nil || next.Task.ID != "task-1" {
		t.Fatal(next, err)
	}
}

func TestAgentTransactionRollbackAndContextCancellation(t *testing.T) {
	store, c, p := agentFixture(t)
	ctx := context.Background()
	stop := errors.New("inject transaction failure")
	err := store.AgentTransaction(ctx, "node-token", p.Identity, func(tx node.AgentTransaction) error {
		a := tx.Agent()
		a.Version = "must-not-commit"
		if err := tx.SaveAgent(a); err != nil {
			return err
		}
		task, err := tx.Task("task-0")
		if err != nil {
			return err
		}
		task.Status = "running"
		task.LeasedAt = 1
		if err := tx.SaveTask(task); err != nil {
			return err
		}
		if err := tx.AppendProgress(&task, []node.Event{{Type: "output", Line: "must rollback", Timestamp: 1}}, 300); err != nil {
			return err
		}
		return stop
	})
	if !errors.Is(err, stop) {
		t.Fatal(err)
	}
	if err := store.AgentTransaction(ctx, "node-token", p.Identity, func(tx node.AgentTransaction) error {
		if tx.Agent().Version == "must-not-commit" {
			t.Fatal("partial node commit")
		}
		task, err := tx.Task("task-0")
		if err != nil {
			return err
		}
		if task.Status != "queued" {
			t.Fatal("partial task commit")
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	var events int
	if err := store.db.QueryRow(`SELECT COUNT(*) FROM control_events`).Scan(&events); err != nil || events != 0 {
		t.Fatal(events, err)
	}
	empty := []string{}
	p.SupportedActions = &empty
	p.WaitSeconds = 30
	deadline, cancel := context.WithTimeout(ctx, 50*time.Millisecond)
	defer cancel()
	if _, err := c.Lease(deadline, "node-token", p); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	// Canceling a poll cannot retain the writer transaction and block heartbeat.
	if _, err := c.Heartbeat(ctx, "node-token", p); err != nil {
		t.Fatal(err)
	}
}

func TestHandoffGraceThenInterruptedTaskFailsWithoutRequeue(t *testing.T) {
	store, c, p := agentFixture(t)
	ctx := context.Background()
	if _, err := c.Lease(ctx, "node-token", p); err != nil {
		t.Fatal(err)
	}
	c.Now = func() time.Time { return time.Unix(1030, 0) }
	if _, err := c.Heartbeat(ctx, "node-token", p); err != nil {
		t.Fatal(err)
	}
	c.Now = func() time.Time { return time.Unix(1061, 0) }
	if _, err := c.Heartbeat(ctx, "node-token", p); err != nil {
		t.Fatal(err)
	}
	if err := store.AgentTransaction(ctx, "node-token", p.Identity, func(tx node.AgentTransaction) error {
		task, err := tx.Task("task-0")
		if err != nil {
			return err
		}
		if task.Status != "failed" || task.CompletedAt != 1061 {
			t.Fatalf("%+v", task)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	next, err := c.Lease(ctx, "node-token", p)
	if err != nil || next.Task.ID != "task-1" {
		t.Fatal(next, err)
	}
}

func TestNodeCredentialCannotUseAnotherNodeIDOrManagementToken(t *testing.T) {
	_, c, p := agentFixture(t)
	ctx := context.Background()
	for _, token := range []string{"management", "join-token", "", "invalid"} {
		if _, err := c.Heartbeat(ctx, token, p); !errors.Is(err, credentials.ErrUnauthorized) {
			t.Fatalf("%q: %v", token, err)
		}
	}
	p.ID = "another-node"
	if _, err := c.Heartbeat(ctx, "node-token", p); !errors.Is(err, credentials.ErrUnauthorized) {
		t.Fatal(err)
	}
}

func TestMissingTaskObservationNeverReconcilesAnOldRunningTask(t *testing.T) {
	store, c, p := agentFixture(t)
	ctx := context.Background()
	if _, err := c.Lease(ctx, "node-token", p); err != nil {
		t.Fatal(err)
	}
	active := "task-0"
	p.ActiveTaskID = &active
	if _, err := c.Heartbeat(ctx, "node-token", p); err != nil {
		t.Fatal(err)
	}
	c.Now = func() time.Time { return time.Unix(1200, 0) }
	p.ActiveTaskID = nil
	reply, err := c.Heartbeat(ctx, "node-token", p)
	if err != nil || reply.ActiveTaskID != "task-0" {
		t.Fatal(reply, err)
	}
	lease, err := c.Lease(ctx, "node-token", p)
	if err != nil || lease.Task != nil {
		t.Fatal(lease, err)
	}
	if err := store.AgentTransaction(ctx, "node-token", p.Identity, func(tx node.AgentTransaction) error {
		task, err := tx.Task("task-0")
		if task.Status != "running" || tx.Agent().ActiveTaskObservedAt != 1000 {
			t.Fatal(task, tx.Agent())
		}
		return err
	}); err != nil {
		t.Fatal(err)
	}
}
