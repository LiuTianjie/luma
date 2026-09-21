//go:build linux || darwin

package process

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
)

func TestMain(m *testing.M) {
	if len(os.Args) > 1 && os.Args[1] == SupervisorArgument {
		os.Exit(Supervise(os.Args[2:]))
	}
	if len(os.Args) > 3 && os.Args[1] == "__test_parent_liveness" {
		_, err := (Runner{Supervisor: os.Args[0]}).Run(context.Background(), node.Command{
			Args: []string{"/bin/sh", "-c", `(sleep 0.5; printf survived > "$MARKER") & printf ready > "$READY"; wait`},
			Env:  map[string]string{"READY": os.Args[2], "MARKER": os.Args[3]},
		}, nil)
		if err != nil {
			os.Exit(1)
		}
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func TestSupervisorCancelsChildrenAfterParentSIGKILL(t *testing.T) {
	root := t.TempDir()
	ready, marker := filepath.Join(root, "ready"), filepath.Join(root, "survived")
	parent := exec.Command(os.Args[0], "__test_parent_liveness", ready, marker)
	if err := parent.Start(); err != nil {
		t.Fatal(err)
	}
	defer parent.Process.Kill()
	deadline := time.Now().Add(5 * time.Second)
	for {
		if _, err := os.Stat(ready); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("supervised child never started")
		}
		time.Sleep(time.Millisecond)
	}
	if err := parent.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	_ = parent.Wait()
	time.Sleep(650 * time.Millisecond)
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("orphaned child survived parent SIGKILL")
	}
}

func TestSupervisorPreservesExitAndTimeout(t *testing.T) {
	runner := Runner{Supervisor: os.Args[0]}
	got, err := runner.Run(context.Background(), node.Command{Args: []string{"/bin/sh", "-c", "printf tool-error; exit 7"}}, nil)
	if err != nil || got.Code != 7 || got.Output != "tool-error" {
		t.Fatalf("result=%+v error=%v", got, err)
	}
	_, err = runner.Run(context.Background(), node.Command{Args: []string{"/bin/sh", "-c", "sleep 10"}, Timeout: 40 * time.Millisecond}, nil)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
}
