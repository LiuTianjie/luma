package manifest

import (
	"strings"
	"testing"

	builddomain "github.com/LiuTianjie/luma/internal/build"
)

func TestBuildHandoffPreservesVolumePlacementAndOverrides(t *testing.T) {
	sha := "abc1234" // Agent returns git rev-parse --short HEAD.
	plan := builddomain.ImageRequest{RegistryHost: "registry.test:5000", Repo: "team/repo"}
	r := builddomain.ControlRequest{RepoURL: "https://github.com/team/repo", Region: "global", Port: 8081, EnvSecrets: map[string]string{}}
	built := builddomain.ImageResult{Kind: "service", SHA: sha, Image: "registry.test:5000/custom/repo:" + sha, Manifest: "name: db\nregion: cn\nnode: lab\nexposure: none\nvolumes: [data:/var/lib/data]\nbuild: {repo: custom/repo}\n"}
	out, compose, err := (Decoder{}).BuiltDeployment(r, plan, built)
	if err != nil || compose {
		t.Fatal(out, compose, err)
	}
	svc, err := (Decoder{}).Service(out.Manifest)
	if err != nil {
		t.Fatal(err)
	}
	if svc.Node != "lab" || svc.Region != "global" || svc.Port != 8081 || svc.Build != nil || len(svc.Volumes) != 1 || svc.Volumes[0] != "data:/var/lib/data" || out.EnvSecrets == nil {
		t.Fatal(svc, out)
	}
	built.Image = "other.test/custom/repo:" + sha
	if _, _, err = (Decoder{}).BuiltDeployment(r, plan, built); err == nil {
		t.Fatal("accepted foreign registry")
	}
	built.Image = "registry.test:5000/custom/repo:latest"
	if _, _, err = (Decoder{}).BuiltDeployment(r, plan, built); err == nil {
		t.Fatal("accepted non-commit tag")
	}
}
func TestComposeBuildHandoffRewritesSharedAliasAndRetainsLocalVolume(t *testing.T) {
	sha := strings.Repeat("b", 40)
	image := "registry.test:5000/team/repo/web:" + sha
	plan := builddomain.ImageRequest{RegistryHost: "registry.test:5000", Repo: "team/repo"}
	request := builddomain.ControlRequest{RepoURL: "https://github.com/team/repo", ComposeSidecar: "deploy/luma.compose.yml", ComposeContent: "services: {web: {image: retired:latest}}", Port: 9999}
	built := builddomain.ImageResult{Kind: "compose", SHA: sha, Images: map[string]string{"web": image}, ImageAliases: map[string]string{"shared:app": image}, ComposeSidecar: request.ComposeSidecar, Manifest: "name: stack\nregion: global\nservices: {web: {exposure: none}, worker: {exposure: none}}\nvolumes: {data: {local: {node: lab, path: /srv/data}}}\n", ComposeContent: "services:\n  web: {build: ., image: 'shared:app'}\n  worker: {image: 'shared:app'}\nvolumes: {data: {}}\n"}
	out, compose, err := (Decoder{}).BuiltDeployment(request, plan, built)
	if err != nil || !compose {
		t.Fatal(out, err)
	}
	if strings.Count(out.ComposeContent, image) != 2 || strings.Contains(out.ComposeContent, "build:") || !strings.Contains(out.Manifest, "/srv/data") || strings.Contains(out.Manifest, "9999") {
		t.Fatal(out)
	}
	built.ComposeSidecar = "wrong.yml"
	if _, _, err := (Decoder{}).BuiltDeployment(request, plan, built); err == nil {
		t.Fatal("accepted wrong selected sidecar")
	}
}

func TestBuildWorkflowRejectsDifferentApplicationBeforeDeploy(t *testing.T) {
	plan := builddomain.ImageRequest{RegistryHost: "registry.test", Repo: "team/repo"}
	_, _, err := (Decoder{}).BuiltDeployment(builddomain.ControlRequest{ExpectedName: "recorded-app"}, plan, builddomain.ImageResult{Kind: "service", SHA: "abc1234", Image: "registry.test/team/repo:abc1234", Manifest: "name: other-app\nregion: global\n"})
	if err == nil {
		t.Fatal("workflow selected another application")
	}
}

func TestKnownBuildTargetSharesReplacementAcrossRepositories(t *testing.T) {
	d := Decoder{}
	original, err := d.BuildReplacement(builddomain.ControlRequest{RepoURL: "https://git.test/original", Manifest: "name: app\n"})
	if err != nil {
		t.Fatal(err)
	}
	for _, request := range []builddomain.ControlRequest{
		{RepoURL: "https://git.test/another", ExpectedName: "app"},
		{RepoURL: "https://git.test/another", ExpectedName: "app", ComposeSidecar: "deploy/stack.yml"},
	} {
		key, err := d.BuildReplacement(request)
		if err != nil || key == "" || key != original {
			t.Fatal(key, original, err)
		}
	}
	sibling, err := d.BuildReplacement(builddomain.ControlRequest{RepoURL: "https://git.test/original", ExpectedName: "sibling"})
	if err != nil || sibling == original {
		t.Fatal(sibling, err)
	}
}
