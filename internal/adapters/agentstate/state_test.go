package agentstate

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/LiuTianjie/luma/internal/node"
)

func TestReceiptIsPrivateAtomicRecoverableAndLocked(t *testing.T) {
	config := filepath.Join(t.TempDir(), "agent.json")
	store, unlock, err := Open(config)
	if err != nil {
		t.Fatal(err)
	}
	if _, other, err := Open(config); err == nil {
		other()
		t.Fatal("two agent processes acquired one configuration")
	}
	r := node.Receipt{Identity: node.Identity{Name: "worker", ID: "node"}, Authority: "https://control.test", TaskID: "task", Status: "succeeded", Message: "done", Result: []byte(`{"path":"/data"}`)}
	if err := store.Save(r); err != nil {
		t.Fatal(err)
	}
	unlock()
	store, unlock, err = Open(config)
	if err != nil {
		t.Fatal(err)
	}
	defer unlock()
	loaded, err := store.Load()
	if err != nil || loaded.TaskID != r.TaskID || string(loaded.Result) != string(r.Result) {
		t.Fatalf("receipt=%+v error=%v", loaded, err)
	}
	if info, err := os.Stat(store.path); err != nil || info.Mode().Perm() != 0600 {
		t.Fatal("receipt permissions not private")
	}
	if err := store.Clear(); err != nil {
		t.Fatal(err)
	}
	if loaded, err := store.Load(); err != nil || loaded != nil {
		t.Fatal("acknowledged receipt was not removed")
	}
}
func TestCorruptReceiptNeverLooksIdle(t *testing.T) {
	store, unlock, err := Open(filepath.Join(t.TempDir(), "agent.json"))
	if err != nil {
		t.Fatal(err)
	}
	defer unlock()
	for _, data := range []string{`{`, `{}`, `{"Status":"succeeded","Payload":{"token":"private"}}`} {
		_ = os.WriteFile(store.path, []byte(data), 0600)
		if _, err := store.Load(); err == nil {
			t.Fatal("corrupt receipt treated as idle")
		}
	}
}
func TestAgentConfigRejectsPublicFilesAndDoesNotExposeToken(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.json")
	_ = os.WriteFile(path, []byte(`{"endpoint":"https://control.test","token":"private-token","nodeName":"worker"}`), 0644)
	if _, err := LoadConfig(path); err == nil {
		t.Fatal("world-readable token accepted")
	}
	_ = os.Chmod(path, 0600)
	if config, err := LoadConfig(path); err != nil || config.NodeName != "worker" {
		t.Fatal(err)
	}
	_ = os.WriteFile(path, []byte(`{"token":"private-token"`), 0600)
	if _, err := LoadConfig(path); err == nil || strings.Contains(err.Error(), "private-token") {
		t.Fatal("invalid config exposed token")
	}
}
