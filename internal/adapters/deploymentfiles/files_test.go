package deploymentfiles

import (
	"context"
	"github.com/LiuTianjie/luma/internal/deployment"
	"os"
	"path/filepath"
	"testing"
)

func TestWritesJobAndRemovesOnlyItsShadow(t *testing.T) {
	base := t.TempDir()
	config := deployment.RenderConfig{StackRoot: filepath.Join(base, "stacks"), RoutesRoot: filepath.Join(base, "routes")}
	f, err := New(config, base)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	job := filepath.Join(config.StackRoot, "app", "app.json")
	shadow := filepath.Join(config.RoutesRoot, "app.yml")
	other := filepath.Join(config.RoutesRoot, "other.yml")
	for _, name := range []string{shadow, other} {
		if err := os.WriteFile(name, []byte("old"), 0644); err != nil {
			t.Fatal(err)
		}
	}
	plan := deployment.Plan{Artifacts: []deployment.Artifact{{Kind: "job", Path: job, Content: `{"Job":{"ID":"app"}}`}}}
	if err := f.Validate(plan, []string{shadow}); err != nil {
		t.Fatal(err)
	}
	paths, err := f.WriteJobs(context.Background(), plan)
	if err != nil || len(paths) != 1 {
		t.Fatal(paths, err)
	}
	info, err := os.Stat(job)
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatal(info, err)
	}
	if _, err := f.ReconcileRoutes(context.Background(), plan, []string{shadow}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(shadow); !os.IsNotExist(err) {
		t.Fatal("shadow still present", err)
	}
	if data, err := os.ReadFile(other); err != nil || string(data) != "old" {
		t.Fatal("unrelated route changed", err)
	}
	if err := f.Validate(plan, []string{filepath.Join(base, "outside.yml")}); err == nil {
		t.Fatal("accepted out-of-root route removal")
	}
}
