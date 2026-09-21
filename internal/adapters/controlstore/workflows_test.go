package controlstore

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"testing"

	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/workflow"
)

func TestWorkflowConcurrentRevisionAndRollback(t *testing.T) {
	dir, db := fixture(t)
	store, err := OpenWriter(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	s := workflow.Service{Repository: store}
	ctx := context.Background()
	zero := int64(0)
	r := workflow.RecordRequest{Name: "app", ExpectedRevision: &zero, CheckRequest: workflow.CheckRequest{Recipe: workflow.Recipe{SchemaVersion: 1, Method: "image-deploy", Argv: []string{"deploy", "app.yml"}}}}
	var group sync.WaitGroup
	results := make(chan error, 16)
	for range 16 {
		group.Go(func() { _, err := s.Record(ctx, "management", r); results <- err })
	}
	group.Wait()
	close(results)
	wins, conflicts := 0, 0
	for err := range results {
		if err == nil {
			wins++
		} else if errors.Is(err, workflow.ErrConflict) {
			conflicts++
		} else {
			t.Fatal(err)
		}
	}
	if wins != 1 || conflicts != 15 {
		t.Fatal(wins, conflicts)
	}
	// Preserve unknown fields and independent collections through later edits.
	if _, err := db.Exec(`UPDATE control_config SET payload=json_set(payload,'$.app.legacyExtension',json('{"keep":true}')) WHERE key='deploymentWorkflows'`); err != nil {
		t.Fatal(err)
	}
	r.ExpectedRevision = nil
	if _, err := s.Record(ctx, "management", r); err != nil {
		t.Fatal(err)
	}
	var raw []byte
	if err := db.QueryRow(`SELECT payload FROM control_config WHERE key='deploymentWorkflows'`).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var records map[string]map[string]any
	_ = json.Unmarshal(raw, &records)
	if records["app"]["legacyExtension"].(map[string]any)["keep"] != true {
		t.Fatal(records)
	}
	stop := errors.New("abort edit")
	_, err = store.UpdateWorkflow(ctx, "management", func(c workflow.Catalog) (workflow.Record, error) { return workflow.Record{}, stop })
	if !errors.Is(err, stop) {
		t.Fatal(err)
	}
	// Rotation after HTTP authorization is checked again inside the transaction.
	if _, err := db.Exec(`UPDATE control_config SET payload='"rotated"' WHERE key='deployToken'`); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Record(ctx, "management", r); !errors.Is(err, credentials.ErrUnauthorized) {
		t.Fatal(err)
	}
	saved, err := s.Get(ctx, "app")
	if err != nil || saved.Revision != 2 {
		t.Fatal(saved, err)
	}
}

func TestWorkflowReadOnlyAndOversizedCatalog(t *testing.T) {
	dir, db := fixture(t)
	store, err := OpenReadOnly(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if _, err = store.UpdateWorkflow(context.Background(), "management", nil); !errors.Is(err, ErrReadOnly) {
		t.Fatal(err)
	}
	if _, err = db.Exec(`INSERT INTO control_config VALUES('deploymentWorkflows',json_object('oversized',printf('%.*c',?, 'x')))`, maxWorkflowCatalog); err != nil {
		t.Fatal(err)
	}
	if _, err = store.Workflows(context.Background()); err == nil {
		t.Fatal("accepted oversized workflow catalog")
	}
}
