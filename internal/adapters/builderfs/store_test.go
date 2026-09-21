package builderfs

import (
	"bytes"
	"context"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	builddomain "github.com/LiuTianjie/luma/internal/build"
)

func artifactRequest(raw []byte) builddomain.ArtifactExportRequest {
	return builddomain.ArtifactExportRequest{LeaseID: "artdl_" + strings.Repeat("a", 32), BuilderTaskID: "builder-task-one", Artifact: builddomain.AnalysisArtifact{Name: "evidence", Digest: hashBytes(raw), MediaType: "application/vnd.lae.evidence+json", SizeBytes: int64(len(raw))}}
}

func TestContentStoreRoundTripLockAndCorruption(t *testing.T) {
	ctx := context.Background()
	base := physicalTemp(t)
	store := Store{Root: filepath.Join(base, "store")}
	raw := []byte(`{"evidence":"fixture"}`)
	request := artifactRequest(raw)
	if err := store.PutArtifact(ctx, bytes.NewReader(raw), request.Artifact); err != nil {
		t.Fatal(err)
	}
	if err := store.PutArtifact(ctx, bytes.NewReader(raw), request.Artifact); err != nil {
		t.Fatal("dedup: ", err)
	}
	content, err := store.OpenArtifact(ctx, request)
	if err != nil {
		t.Fatal(err)
	}
	data, err := io.ReadAll(content)
	if err != nil || !bytes.Equal(data, raw) {
		t.Fatal("wrong content: ", err)
	}
	if _, err := store.OpenArtifact(ctx, request); err == nil {
		t.Fatal("execution lock was not held by reader")
	}
	content.Close()
	content.Close()
	content, err = store.OpenArtifact(ctx, request)
	if err != nil {
		t.Fatal("lock not released: ", err)
	}
	content.Close()
	namespace, _ := request.Artifact.StoreNamespace()
	name, _ := contentPath(namespace, request.Artifact.Digest, ".json")
	writeFile(t, filepath.Join(store.Root, name), strings.Repeat("x", len(raw)), 0600)
	if _, err := store.OpenArtifact(ctx, request); err == nil {
		t.Fatal("corrupt content returned")
	}
	if err := store.PutArtifact(ctx, bytes.NewReader(raw), request.Artifact); err == nil {
		t.Fatal("corruption silently overwritten")
	}
	unchanged, _ := os.ReadFile(filepath.Join(store.Root, name))
	if string(unchanged) != strings.Repeat("x", len(raw)) {
		t.Fatal("corrupt path overwritten")
	}
}

func TestSnapshotStoreReadsWithoutMovingData(t *testing.T) {
	base := physicalTemp(t)
	source := filepath.Join(base, "source")
	writeFile(t, filepath.Join(source, "app"), "working tree", 0600)
	raw := createFile(t, source, filepath.Join(base, "source.tar"))
	digest := hashBytes(raw)
	store := Store{Root: filepath.Join(base, "store")}
	ctx := context.Background()
	if err := store.PutSnapshot(ctx, bytes.NewReader(raw), digest, testLimits); err != nil {
		t.Fatal(err)
	}
	content, err := store.OpenSnapshot(ctx, digest, testLimits)
	if err != nil {
		t.Fatal(err)
	}
	defer content.Close()
	if err := Extract(ctx, content, filepath.Join(base, "extracted"), digest, testLimits); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(filepath.Join(base, "extracted", "app")); string(data) != "working tree" {
		t.Fatal("contents lost")
	}
}

func TestContentStoreRejectsPathsBindingsAndSymlinks(t *testing.T) {
	ctx := context.Background()
	raw := []byte(`{}`)
	valid := artifactRequest(raw)
	for _, field := range []string{"lease", "task", "name", "type", "digest", "size"} {
		t.Run(field, func(t *testing.T) {
			r := valid
			switch field {
			case "lease":
				r.LeaseID = "../bad"
			case "task":
				r.BuilderTaskID = "bad?"
			case "name":
				r.Artifact.Name = "../other"
			case "type":
				r.Artifact.MediaType = "text/plain"
			case "digest":
				r.Artifact.Digest = "../bad"
			case "size":
				r.Artifact.SizeBytes++
			}
			base := physicalTemp(t)
			store := Store{Root: filepath.Join(base, "store")}
			if err := store.PutArtifact(ctx, bytes.NewReader(raw), valid.Artifact); err != nil {
				t.Fatal(err)
			}
			if c, err := store.OpenArtifact(ctx, r); err == nil {
				c.Close()
				t.Fatal("invalid descriptor accepted")
			}
		})
	}
	for _, kind := range []string{"root-link", "directory-link", "file-link", "directory-permission"} {
		t.Run(kind, func(t *testing.T) {
			base := physicalTemp(t)
			store := Store{Root: filepath.Join(base, "store")}
			if err := store.PutArtifact(ctx, bytes.NewReader(raw), valid.Artifact); err != nil {
				t.Fatal(err)
			}
			namespace, _ := valid.Artifact.StoreNamespace()
			name, _ := contentPath(namespace, valid.Artifact.Digest, ".json")
			target := filepath.Join(store.Root, name)
			switch kind {
			case "root-link":
				target = store.Root
			case "directory-link", "directory-permission":
				target = filepath.Dir(target)
			}
			if kind == "directory-permission" {
				if err := os.Chmod(target, 0777); err != nil {
					t.Fatal(err)
				}
			} else {
				if err := os.Rename(target, target+"-real"); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(target+"-real", target); err != nil {
					t.Fatal(err)
				}
			}
			if c, err := store.OpenArtifact(ctx, valid); err == nil {
				c.Close()
				t.Fatal("unsafe path accepted")
			}
		})
	}
}
