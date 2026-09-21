package controlstore

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/deployment"
)

func reserveLocal(t *testing.T, s *Store, id string) builddomain.Run {
	t.Helper()
	run, err := s.ReserveLocalBuild(context.Background(), "management", builddomain.Submission{Run: builddomain.Run{ID: id, Source: "https://git.test/team/app", ProjectKey: "team/app"}, Request: builddomain.ControlRequest{RepoURL: "https://git.test/team/app", RegistryHost: "registry.test", Platform: "linux/arm64", Queue: true, BuildTimeout: 60}})
	if err != nil {
		t.Fatal(err)
	}
	return run
}
func localResult(id string) builddomain.ImageResult {
	return builddomain.ImageResult{Kind: "service", SHA: builddomain.LocalBuildTag(id), Image: "registry.test/team/app:" + builddomain.LocalBuildTag(id), Manifest: "name: app\nregion: global\nexposure: none\n"}
}
func TestLocalUploadCanCompleteOnlyOnceAndUsesProjectQueue(t *testing.T) {
	s, _, _ := buildFixture(t)
	ctx := context.Background()
	local := reserveLocal(t, s, "build-local")
	if local.HoldsProject() {
		t.Fatal("upload blocks project queue")
	}
	remote := submitBuild(t, s, "remote", "team/app", "")
	claimBuild(t, s, remote.ID)
	if err := s.RecoverBuilds(ctx); err != nil {
		t.Fatal(err)
	}
	read, err := s.ReadBuild(ctx, local.ID)
	if err != nil || read.Status != "running" {
		t.Fatal(read, err)
	}
	var wg sync.WaitGroup
	var accepted atomic.Int32
	for range 8 {
		wg.Go(func() {
			_, err := s.CompleteLocalBuild(ctx, "management", local.ID, builddomain.LocalCompleteRequest{BuildResult: localResult(local.ID), EnvSecrets: map[string]string{}})
			if err == nil {
				accepted.Add(1)
			} else if !errors.Is(err, builddomain.ErrState) {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if accepted.Load() != 1 {
		t.Fatal(accepted.Load())
	}
	work, err := s.ClaimBuild(ctx)
	if err != nil || work == nil || work.Run.ID != local.ID || work.LocalResult == nil || work.Request.EnvSecrets == nil {
		t.Fatal(work, err)
	}
	if err := s.BeginBuildDeployment(ctx, local.ID); err != nil {
		t.Fatal(err)
	}
	if err := s.FinishBuild(ctx, local.ID, "succeeded", map[string]any{"service": "app"}, "done"); err != nil {
		t.Fatal(err)
	}
	got, replayed, err := s.FailLocalBuild(ctx, "management", local.ID, "late")
	if err != nil || !replayed || got.Status != "succeeded" {
		t.Fatal(got, replayed, err)
	}
}
func TestLocalUploadExpirationAndFailure(t *testing.T) {
	s, _, _ := buildFixture(t)
	ctx := context.Background()
	r := reserveLocal(t, s, "build-expired")
	if _, err := s.writer.Exec(`UPDATE control_entities SET payload=json_set(payload,'$.expiresAt',1) WHERE kind='buildRuns' AND id=?`, r.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := s.CompleteLocalBuild(ctx, "management", r.ID, builddomain.LocalCompleteRequest{BuildResult: localResult(r.ID)}); !errors.Is(err, builddomain.ErrState) {
		t.Fatal(err)
	}
	claimBuild(t, s, "")
	got, err := s.ReadBuild(ctx, r.ID)
	if err != nil || got.Status != "failed" {
		t.Fatal(got, err)
	}
	r = reserveLocal(t, s, "build-fail")
	got, replayed, err := s.FailLocalBuild(ctx, "management", r.ID, "compiler error")
	if err != nil || replayed || got.Status != "failed" {
		t.Fatal(got, replayed, err)
	}
	if _, err := s.CompleteLocalBuild(ctx, "management", r.ID, builddomain.LocalCompleteRequest{BuildResult: localResult(r.ID)}); !errors.Is(err, builddomain.ErrState) {
		t.Fatal(err)
	}
}
func TestLocalPlatformReservationNeedsAllTargetsAndNoRemoteBuilder(t *testing.T) {
	s, _, _ := buildFixture(t)
	ctx := context.Background()
	if _, err := s.writer.Exec(`INSERT OR REPLACE INTO control_config(key,payload) VALUES('build','{"registryHost":"REGISTRY.test","defaultNode":"offline-builder"}'); UPDATE control_entities SET payload=json_set(payload,'$.region','global','$.agent.os','linux','$.agent.arch','arm64') WHERE kind='nodes' AND id='worker'`); err != nil {
		t.Fatal(err)
	}
	input := builddomain.LocalPrepareRequest{ControlRequest: builddomain.ControlRequest{RepoURL: "team/app", SourceRevision: "abc-dirty"}, TargetNodes: []string{"worker"}}
	plan, err := s.PlanLocalBuild(ctx, input, deployment.RenderConfig{})
	if err != nil || plan.Request.Platform != "linux/arm64" || plan.Request.RegistryHost != "registry.test" || plan.Request.BuildNode != "offline-builder" || plan.ProjectKey != "team/app" {
		t.Fatal(plan, err)
	}
	if _, err := s.writer.Exec(`INSERT INTO control_entities(kind,id,payload) VALUES('nodes','x86','{"region":"cn","agent":{"status":"ready","os":"linux","arch":"amd64"}}')`); err != nil {
		t.Fatal(err)
	}
	input.TargetRegions = []string{"cn"}
	mixed, err := s.PlanLocalBuild(ctx, input, deployment.RenderConfig{})
	if err != nil || mixed.Request.Platform != "linux/amd64,linux/arm64" {
		t.Fatal(mixed, err)
	}
	input.TargetRegions = nil
	input.Platform = "linux/amd64"
	if _, err := s.PlanLocalBuild(ctx, input, deployment.RenderConfig{}); !errors.Is(err, builddomain.ErrInput) {
		t.Fatal(err)
	}
	input.Platform = ""
	input.TargetNodes = append(input.TargetNodes, "missing")
	if _, err := s.PlanLocalBuild(ctx, input, deployment.RenderConfig{}); !errors.Is(err, builddomain.ErrInput) {
		t.Fatal(err)
	}
}
