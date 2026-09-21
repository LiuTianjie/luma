package hostadmin

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/LiuTianjie/luma/internal/node"
)

func TestCNIOnlyDeletesStaleRulesShadowingAnActiveRequestedPort(t *testing.T) {
	rule := func(id, port string) string {
		return fmt.Sprintf(`-A CNI-HOSTPORT-DNAT -p tcp -m multiport --dports %s -m comment --comment "dnat name: \"nomad\" id: \"%s\"" -j CNI-DN-%s`, port, id, id)
	}
	saved := strings.Join([]string{rule("stale", "8080"), rule("unrelated", "9999"), rule("active", "8080"), rule("later-stale", "8080")}, "\n")
	var deletes [][]string
	e := testExecutor(t, "linux", func(_ context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		if c.Args[0] == "docker" {
			return node.CommandResult{Output: "active\n"}, nil
		}
		if c.Args[5] == "-S" {
			return node.CommandResult{Output: saved}, nil
		}
		deletes = append(deletes, c.Args)
		return node.CommandResult{}, nil
	})
	out, err := e.Execute(context.Background(), task("repair-nomad-cni-hostports", map[string]any{"ports": []int{8080}}), nil)
	if err != nil || len(deletes) != 1 || !strings.Contains(string(out.Result), `"deleted":1`) {
		t.Fatal(deletes, string(out.Result), err)
	}
	if !strings.Contains(strings.Join(deletes[0], " "), `id: "stale"`) || deletes[0][5] != "-D" {
		t.Fatal(deletes)
	}
	if _, ok := parseCNIRule(`-A OTHER -p tcp --dports 8080 -j X`); ok {
		t.Fatal("accepted unrelated chain")
	}
	words, err := ruleWords(`--comment "$(touch /tmp/no); a 'quote'"`)
	if err != nil || len(words) != 2 || words[1] != `$(touch /tmp/no); a 'quote'` {
		t.Fatal(words, err)
	}
}
func TestCNINoActiveAllocationsNeverTouchesIPTables(t *testing.T) {
	e := testExecutor(t, "linux", func(_ context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		if c.Args[0] != "docker" {
			t.Fatal(c.Args)
		}
		return node.CommandResult{}, nil
	})
	out, err := e.Execute(context.Background(), task("repair-nomad-cni-hostports", map[string]any{"ports": []int{8080}}), nil)
	if err != nil || !strings.Contains(string(out.Result), `"deleted":0`) {
		t.Fatal(string(out.Result), err)
	}
}
