package controlstore

import (
	"context"
	"testing"
)

func TestImageBuilderDiscoveryPreservesOrdinaryStorageNodes(t *testing.T) {
	dir, db := fixture(t)
	for name, raw := range map[string]string{
		"worker":  `{"nodeId":"worker-id","region":"cn","agent":{"status":"ready","os":"linux","arch":"amd64"}}`,
		"builder": `{"nodeId":"builder-id","region":"cn","agent":{"status":"ready"}}`,
	} {
		if _, err := db.Exec(`INSERT INTO control_entities(kind,id,payload) VALUES('nodes',?,?)`, name, raw); err != nil {
			t.Fatal(err)
		}
	}
	store, err := OpenReadOnly(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	snapshot, err := store.DeploymentSnapshot(context.Background(), "service", "app")
	if err != nil {
		t.Fatal(err)
	}
	for _, n := range snapshot.Nodes {
		if !n.Ready || n.Builder != (n.Name == "builder") {
			t.Fatalf("wrong storage placement candidate: %+v", n)
		}
	}
	worker := snapshot.ImageNodes["worker"]
	if worker.DirectEgress || worker.Builder || worker.Platform != "linux/amd64" {
		t.Fatal(worker)
	}
	// Legacy catalogs can contain empty aliases and empty entries in build.nodes.
	r, _ := readDocument([]byte(`{"agent":{"status":"ready"}}`))
	if deploymentImageNode("worker", r, document{}, []string{""}, []string{""}, true).Builder {
		t.Fatal("empty alias elected a Builder")
	}
}

func TestImageNodePlatformUsesPersistedAndAgentFormats(t *testing.T) {
	for _, raw := range []string{
		`{"nodePlatform":"darwin/arm64"}`,
		`{"platform":{"OS":"Darwin","Architecture":"aarch64"}}`,
		`{"agent":{"os":"mac os x","arch":"arm64/v8"}}`,
		`{"nomadAttributes":{"kernel.name":"linux","cpu.arch":"arm64"}}`,
	} {
		r, err := readDocument([]byte(raw))
		if err != nil {
			t.Fatal(err)
		}
		if got := deploymentImageNode("worker", r, document{}, nil, nil, true).Platform; got != "linux/arm64" {
			t.Fatalf("%s: %s", raw, got)
		}
	}
}
