package builderfs

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	builddomain "github.com/LiuTianjie/luma/internal/build"
)

// The migration harness supplies archives created by the old implementation.
// This test executes only Go; Python is never invoked by a runtime component.
func TestMigrationContract(t *testing.T) {
	base := os.Getenv("LUMA_TEST_BUILDER_FIXTURE")
	if base == "" {
		t.Skip("run tests.test_go_builder_storage_contract for cross-language fixtures")
	}
	raw, err := os.ReadFile(filepath.Join(base, "expected.json"))
	if err != nil {
		t.Fatal(err)
	}
	var expected struct {
		Snapshot   builddomain.Snapshot         `json:"snapshot"`
		SnapshotID string                       `json:"snapshotId"`
		Artifact   builddomain.AnalysisArtifact `json:"artifact"`
	}
	if err := json.Unmarshal(raw, &expected); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	store := Store{Root: filepath.Join(base, "store")}
	request := builddomain.ArtifactExportRequest{LeaseID: "artdl_" + strings.Repeat("a", 32), BuilderTaskID: "task-one", Artifact: expected.Artifact}
	if os.Getenv("LUMA_TEST_BUILDER_LOCKED") == "1" {
		if content, err := store.OpenArtifact(ctx, request); err == nil {
			content.Close()
			t.Fatal("ignored legacy cleanup execution lock")
		}
		return
	}
	legacy, err := store.OpenSnapshot(ctx, expected.Snapshot.Digest, testLimits)
	if err != nil {
		t.Fatal(err)
	}
	result, err := Inspect(ctx, legacy, testLimits)
	if err != nil {
		legacy.Close()
		t.Fatal(err)
	}
	if result != expected.Snapshot {
		t.Fatalf("legacy snapshot mismatch: got %+v want %+v", result, expected.Snapshot)
	}
	if err := Extract(ctx, legacy, filepath.Join(base, "legacy-materialized"), result.Digest, testLimits); err != nil {
		legacy.Close()
		t.Fatal(err)
	}
	legacy.Close()
	id, err := builddomain.SnapshotID("task-one", result.Digest)
	if err != nil || id != expected.SnapshotID {
		t.Fatalf("snapshot binding changed: %s, %v", id, err)
	}
	other, _ := builddomain.SnapshotID("task-two", result.Digest)
	if other == id {
		t.Fatal("snapshot handle not task-scoped")
	}
	created, err := Create(ctx, filepath.Join(base, "source"), filepath.Join(base, "go.tar"), testLimits)
	if err != nil {
		t.Fatal(err)
	}
	if created.TreeDigest != expected.Snapshot.TreeDigest || created.SourceBytes != expected.Snapshot.SourceBytes {
		t.Fatalf("Go logical tree differs: got %+v want %+v", created, expected.Snapshot)
	}
	file, err := os.Open(filepath.Join(base, "go.tar"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.PutSnapshot(ctx, file, created.Digest, testLimits); err != nil {
		file.Close()
		t.Fatal(err)
	}
	file.Close()
	artifact, err := store.OpenArtifact(ctx, request)
	if err != nil {
		t.Fatal(err)
	}
	body, err := io.ReadAll(artifact)
	artifact.Close()
	if err != nil || hashBytes(body) != expected.Artifact.Digest {
		t.Fatal("legacy artifact changed: ", err)
	}
	newBody := []byte(`{"origin":"Go","evidence":"新制品"}`)
	newArtifact := artifactRequest(newBody).Artifact
	if err := store.PutArtifact(ctx, bytes.NewReader(newBody), newArtifact); err != nil {
		t.Fatal(err)
	}
	output, err := json.Marshal(map[string]any{"snapshot": created, "artifact": newArtifact})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(base, "go-result.json"), output, 0600); err != nil {
		t.Fatal(err)
	}
}
