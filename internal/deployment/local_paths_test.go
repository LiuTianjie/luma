package deployment

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/LiuTianjie/luma/internal/node"
	"github.com/LiuTianjie/luma/internal/storage"
)

func TestLocalVolumePathsStayOnOwnerAndPrepareOnceWithoutReset(t *testing.T) {
	cp := Compose{Volumes: map[string]storage.Volume{
		"new":     {Name: "new", LocalNode: "worker", LocalPath: "/srv/data", Initialize: "empty"},
		"adopted": {Name: "adopted", LocalNode: "worker", LocalPath: "/srv/data/", Adopted: true},
	}}
	snapshot := Snapshot{ImageNodes: map[string]ImageNode{"worker": {Ready: true, Capabilities: []string{"managed-volume-path"}}}}
	plans, err := planLocalPaths(cp, snapshot, true)
	if err != nil || len(plans) != 2 {
		t.Fatal(plans, err)
	}
	calls := 0
	c := Coordinator{NodeTasks: imageTaskFunc(func(_ context.Context, _ string, owner node.TaskOwner, name, action, capability string, payload json.RawMessage) (json.RawMessage, error) {
		calls++
		if name != "worker" || owner.OperationID != "deploy-local" || action != "prepare-managed-volume-path" || capability != "managed-volume-path" {
			t.Fatal("wrong preparation owner")
		}
		var values map[string]any
		_ = json.Unmarshal(payload, &values)
		if values["root"] != "/srv" || values["relative"] != "data" || values["preserveExisting"] != true {
			t.Fatal(values)
		}
		return json.RawMessage(`{"path":"/srv/data"}`), nil
	})}
	results, err := c.prepareLocalPaths(context.Background(), "management", Prepared{Record: Record{Kind: "compose", Slug: "app", OperationID: "deploy-local"}, LocalPaths: plans})
	if err != nil || len(results) != 2 || calls != 1 {
		t.Fatal(results, err, calls)
	}
	if _, err := planLocalPaths(cp, Snapshot{}, true); err == nil {
		t.Fatal("prepared data on an unregistered node")
	}
}
