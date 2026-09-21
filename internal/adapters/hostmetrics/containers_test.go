package hostmetrics

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
	"github.com/LiuTianjie/luma/internal/observability"
)

type metricsRunner func(context.Context, node.Command, func(string)) (node.CommandResult, error)

func (f metricsRunner) Run(ctx context.Context, c node.Command, emit func(string)) (node.CommandResult, error) {
	return f(ctx, c, emit)
}

func TestContainerStatsFilterNomadAndParseDockerUnits(t *testing.T) {
	s := ContainerSampler{Docker: "docker", Runner: metricsRunner(func(_ context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		if c.Args[1] == "ps" {
			return node.CommandResult{Output: "0123456789ab\tweb\talloc-a\tapi\tweb\tapi\nabcdefabcdef\tunmanaged\t<no value>\t<no value>\t<no value>\t<no value>\n111111111111\tjobless\talloc-b\t<no value>\t<no value>\t<no value>\n"}, nil
		}
		if strings.Contains(strings.Join(c.Args, " "), "abcdefabcdef") {
			t.Fatal("unmanaged container sampled")
		}
		return node.CommandResult{Output: `{"ID":"0123456789abcdef","CPUPerc":"125.234%","MemPerc":"3.145%","MemUsage":"1.5GiB / 4GB"}` + "\n" + `{"ID":"111111111111","CPUPerc":"NaN%","MemPerc":"+Inf%","MemUsage":"invalid"}`}, nil
	})}
	items, ok := s.sample(context.Background())
	if !ok || len(items) != 2 {
		t.Fatalf("%+v %v", items, ok)
	}
	a := items[0]
	if a.Service != "api" || a.NomadAllocID != "alloc-a" || a.TaskID != "web" || a.CPUPercent == nil || *a.CPUPercent != 125.23 || a.MemoryUsageBytes != 1610612736 || a.MemoryLimitBytes != 4000000000 {
		t.Fatalf("%+v", a)
	}
	b := items[1]
	if b.Service != "nomad:alloc-b" || b.CPUPercent != nil || b.MemoryPercent != nil || b.TaskID != "" {
		t.Fatalf("%+v", b)
	}
	s.items = items
	first := s.Snapshot()
	*first[0].CPUPercent = 999
	first[0].Service = "mutated"
	if s.Snapshot()[0].Service != "api" || *s.Snapshot()[0].CPUPercent != 125.23 {
		t.Fatal("snapshot changed sampler state")
	}
}

func TestContainerSnapshotDoesNotWaitForDocker(t *testing.T) {
	entered := make(chan struct{})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s := ContainerSampler{Docker: "docker", items: []observability.ContainerStat{{Service: "previous"}}, Runner: metricsRunner(func(ctx context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		close(entered)
		<-ctx.Done()
		return node.CommandResult{}, ctx.Err()
	})}
	done := make(chan struct{})
	go func() { defer close(done); s.Run(ctx) }()
	<-entered
	start := time.Now()
	if got := s.Snapshot(); len(got) != 1 || got[0].Service != "previous" {
		t.Fatal(got)
	}
	if time.Since(start) > 100*time.Millisecond {
		t.Fatal("snapshot blocked on Docker")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("sampler ignored shutdown")
	}
}

func TestContainerStatsBoundsAndPartialFailure(t *testing.T) {
	var ps strings.Builder
	for i := 0; i < 250; i++ {
		fmt.Fprintf(&ps, "%012x\tname-%d\talloc-%d\tapp\tweb\tapp\n", i, i, i)
	}
	s := ContainerSampler{Docker: "docker", Runner: metricsRunner(func(_ context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		if c.Args[1] == "ps" {
			return node.CommandResult{Output: ps.String()}, nil
		}
		if len(c.Args) != 205 {
			t.Fatalf("unbounded Docker stats arguments %d", len(c.Args))
		}
		return node.CommandResult{Code: 1}, nil
	})}
	items, ok := s.sample(context.Background())
	if ok || len(items) != 200 {
		t.Fatalf("lost discovery on stats failure: %d %v", len(items), ok)
	}
}
