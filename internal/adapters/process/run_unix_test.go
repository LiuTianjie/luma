//go:build linux || darwin

package process

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
)

func TestProcessCancellationKillsDescendants(t *testing.T) {
	root := t.TempDir()
	ready, marker := filepath.Join(root, "ready"), filepath.Join(root, "survived")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		_, err := (Runner{}).Run(ctx, node.Command{Args: []string{"/bin/sh", "-c", `(sleep 0.4; printf survived > "$MARKER") & printf ready > "$READY"; wait`}, Env: map[string]string{"READY": ready, "MARKER": marker}}, nil)
		done <- err
	}()
	deadline := time.Now().Add(time.Second)
	for {
		if _, err := os.Stat(ready); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("helper did not start")
		}
		time.Sleep(time.Millisecond)
	}
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("cancellation did not reap process")
	}
	time.Sleep(450 * time.Millisecond)
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("descendant survived cancellation")
	}
}
func TestProcessOutputRedactsAcrossWritesAndBoundsMemory(t *testing.T) {
	var lines []string
	w := &boundedOutput{secrets: []string{"private-secret"}, emit: func(line string) { lines = append(lines, line) }}
	_, _ = w.Write([]byte(strings.Repeat("x", 3998) + "private-"))
	_, _ = w.Write([]byte("secret\n"))
	_, _ = w.Write([]byte(strings.Repeat("a", outputLimit*2) + "private-secret"))
	w.flush()
	if len(w.text()) > outputLimit || strings.Contains(w.text(), "private-") || strings.Contains(strings.Join(lines, ""), "private-") {
		t.Fatal("output redaction or bound failed")
	}
	if !strings.Contains(strings.Join(lines, ""), "[REDACTED]") {
		t.Fatal("secret was not redacted")
	}
}
func TestProcessTimeoutAndExitCode(t *testing.T) {
	_, err := (Runner{}).Run(context.Background(), node.Command{Args: []string{"/bin/sh", "-c", "sleep 5"}, Timeout: 20 * time.Millisecond}, nil)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	result, err := (Runner{}).Run(context.Background(), node.Command{Args: []string{"/bin/sh", "-c", "printf error; exit 7"}}, nil)
	if err != nil || result.Code != 7 || result.Output != "error" {
		t.Fatalf("result=%+v error=%v", result, err)
	}
}

func TestProcessCanUseAnIsolatedEnvironment(t *testing.T) {
	t.Setenv("LUMA_PROCESS_INHERITED_SECRET", "must-not-enter-analyzer")
	for _, env := range []map[string]string{nil, {"LUMA_ALLOWED_VALUE": "expected"}} {
		result, err := (Runner{}).Run(context.Background(), node.Command{Args: []string{"/usr/bin/env"}, ClearEnv: true, Env: env}, nil)
		if err != nil || result.Code != 0 {
			t.Fatalf("isolated environment: %+v %v", result, err)
		}
		if strings.Contains(result.Output, "LUMA_PROCESS_INHERITED_SECRET") || strings.Contains(result.Output, "must-not-enter-analyzer") {
			t.Fatal("inherited credential entered isolated command")
		}
		if env == nil && result.Output != "" {
			t.Fatalf("empty environment was inherited: %q", result.Output)
		}
		if env != nil && result.Output != "LUMA_ALLOWED_VALUE=expected\n" {
			t.Fatalf("unexpected isolated environment: %q", result.Output)
		}
	}
}
