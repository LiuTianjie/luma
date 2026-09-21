package controlstore

import (
	"context"
	"testing"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/deployment"
)

func TestBuildPlanSingleRepositoryURLAndExplicitDirect(t *testing.T) {
	s, _, _ := buildFixture(t)
	if _, err := s.writer.Exec(`INSERT OR REPLACE INTO control_config(key,payload) VALUES('build','{"defaultNode":"worker"}')`); err != nil {
		t.Fatal(err)
	}
	if _, err := s.writer.Exec(`UPDATE control_entities SET payload=json_set(payload,'$.region','cn','$.agent.os','linux','$.agent.arch','arm64') WHERE kind='nodes' AND id='worker'`); err != nil {
		t.Fatal(err)
	}
	direct := ""
	for source, want := range map[string]string{"https://git.test/repo.git": "git.test/repo", "http://127.0.0.1:9123/repo.git": "9123/repo", "git@git.test:team/repo.git": "team/repo", "team/repo": "team/repo"} {
		p, err := s.PlanBuild(context.Background(), builddomain.ControlRequest{RepoURL: source, BuildNode: "worker", RegistryHost: "registry.test", Proxy: &direct}, deployment.RenderConfig{})
		if err != nil {
			t.Fatal(source, err)
		}
		if p.ProjectKey != want || p.Image.Repo != want || p.Image.Proxy != "" || p.Image.TargetPlatformsByNode["worker"] != "linux/arm64" {
			t.Fatal(p)
		}
	}
}
