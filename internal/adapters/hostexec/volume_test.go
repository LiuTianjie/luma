package hostexec

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestVolumeLifecyclePreservesAdoptedData(t *testing.T) {
	root := t.TempDir()
	p := volumePath{Root: root, Relative: "app/data", PreserveExisting: true}
	path, err := volumeOperation(context.Background(), p, false)
	if err != nil {
		t.Fatal(err)
	}
	if info, _ := os.Stat(path); info.Mode().Perm() != 0777 {
		t.Fatal("new data directory is not writable")
	}
	_ = os.Chmod(path, 0700)
	_ = os.WriteFile(filepath.Join(path, "record"), []byte("database"), 0600)
	if _, err := volumeOperation(context.Background(), p, false); err != nil {
		t.Fatal(err)
	}
	if info, _ := os.Stat(path); info.Mode().Perm() != 0700 {
		t.Fatal("adoption changed database permissions")
	}
	p.PreserveExisting = false
	if _, err := volumeOperation(context.Background(), p, false); err != nil {
		t.Fatal(err)
	}
	if info, _ := os.Stat(path); info.Mode().Perm() != 0777 {
		t.Fatal("managed path permissions not reconciled")
	}
	if _, err := volumeOperation(context.Background(), p, true); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("volume was not removed")
	}
	if _, err := volumeOperation(context.Background(), p, true); err != nil {
		t.Fatal("removal was not idempotent")
	}
}
func TestVolumeRejectsTraversalAndSymlinkEscape(t *testing.T) {
	root, outside := t.TempDir(), t.TempDir()
	_ = os.WriteFile(filepath.Join(outside, "keep"), []byte("data"), 0600)
	_ = os.Symlink(outside, filepath.Join(root, "link"))
	for _, relative := range []string{".", "", "../outside", "/absolute", "link", "link/subdir"} {
		for _, remove := range []bool{false, true} {
			if _, err := volumeOperation(context.Background(), volumePath{Root: root, Relative: relative}, remove); err == nil {
				t.Fatalf("accepted %q remove=%v", relative, remove)
			}
		}
	}
	if _, err := os.Stat(filepath.Join(outside, "keep")); err != nil {
		t.Fatal("escaped the configured root")
	}
}
