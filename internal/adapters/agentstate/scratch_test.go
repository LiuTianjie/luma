package agentstate

import (
	"os"
	"path/filepath"
	"testing"
)

func TestScratchPurgesCrashResidueWithoutFollowingLinks(t *testing.T) {
	config := filepath.Join(t.TempDir(), "agent.json")
	root, err := Scratch(config)
	if err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(t.TempDir(), "keep")
	if err := os.WriteFile(out, []byte("safe"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(root, "auth"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "auth", "config.json"), []byte("expired credential"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(out, filepath.Join(root, "outside")); err != nil {
		t.Fatal(err)
	}
	if _, err := Scratch(config); err != nil {
		t.Fatal(err)
	}
	entries, _ := os.ReadDir(root)
	if len(entries) != 0 {
		t.Fatal("scratch residue retained")
	}
	if _, err := os.Stat(out); err != nil {
		t.Fatal("followed scratch symlink")
	}
	if err := os.Remove(root); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Dir(out), root); err != nil {
		t.Fatal(err)
	}
	if _, err := Scratch(config); err == nil {
		t.Fatal("accepted substituted scratch root")
	}
}
