package contexts_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/LiuTianjie/luma/internal/adapters/contexts"
)

func TestContextRoundTripPrivateAndRedacted(t *testing.T) {
	root := t.TempDir()
	store, _ := contexts.New(root)
	want := contexts.Context{ClusterID: "cluster-1", Endpoint: "https://control.test/", Token: "do-not-print", Insecure: true, ResolveIP: "127.0.0.1"}
	if err := store.Save(want); err != nil {
		t.Fatal(err)
	}
	got, err := store.Load("")
	if err != nil || got.Token != want.Token || got.Endpoint != "https://control.test" {
		t.Fatalf("round trip failed: %v", err)
	}
	info, err := os.Stat(filepath.Join(root, "contexts", "cluster-1.json"))
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatal("credential file permissions")
	}
	if err := os.WriteFile(filepath.Join(root, "contexts", "corrupt.json"), []byte(`{`), 0600); err != nil {
		t.Fatal(err)
	}
	items, err := store.List()
	if err != nil || len(items) != 1 || !items[0].Current {
		t.Fatalf("list=%v err=%v", items, err)
	}
	data, _ := json.Marshal(items)
	if strings.Contains(string(data), want.Token) || strings.Contains(string(data), `"token"`) {
		t.Fatal("token leaked")
	}
	if err := store.Use("missing"); err == nil {
		t.Fatal("selected nonexistent context")
	}
	if _, err := store.Load("../outside"); err == nil {
		t.Fatal("path traversal accepted")
	}
}
