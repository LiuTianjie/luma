package controlapp

import (
	"bytes"
	"context"
	"strings"
	"testing"
)

func TestMigrationProcessRequiresExplicitModeLoopbackAndAgentTLS(t *testing.T) {
	for _, args := range [][]string{
		{}, {"--state-dir", t.TempDir()}, {"--read-only"},
		{"--state-dir", t.TempDir(), "--read-only", "--agent-tasks"},
		{"--state-dir", t.TempDir(), "--agent-tasks"},
		{"--state-dir", t.TempDir(), "--read-only", "--tls-cert", "missing.pem"},
		{"--read-only", "--state-dir", t.TempDir(), "--listen", "0.0.0.0:8080"},
		{"--read-only", "--state-dir", t.TempDir(), "--listen", "localhost:8080"},
		{"--read-only", "--state-dir", t.TempDir(), "--listen", "[::]:8080"},
	} {
		var out, errout bytes.Buffer
		if Run(context.Background(), args, &out, &errout, "test") == 0 || out.Len() != 0 {
			t.Fatalf("accepted %v: %s", args, out.String())
		}
	}
	var out, errout bytes.Buffer
	if Run(context.Background(), []string{"--version"}, &out, &errout, "1.2.3") != 0 || strings.TrimSpace(out.String()) != "1.2.3" {
		t.Fatal(out.String(), errout.String())
	}
}
