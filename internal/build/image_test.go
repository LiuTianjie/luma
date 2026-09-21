package build

import "testing"

func TestTargetArchitectureComesFromDeploymentNotBuilder(t *testing.T) {
	r := ImageRequest{TargetPlatformsByNode: map[string]string{"arm": "linux/arm64"}, TargetPlatformsByRegion: map[string][]string{"cn": {"linux/amd64", "linux/arm64"}}}
	for _, c := range []struct{ node, region, requested, want string }{{"arm", "cn", "linux/amd64,linux/arm64", "linux/arm64"}, {"", "cn", "", "linux/amd64,linux/arm64"}} {
		got, err := TargetPlatform(r, c.node, c.region, c.requested)
		if err != nil || got != c.want {
			t.Fatal(got, err)
		}
	}
	for _, c := range []struct{ node, region, requested string }{{"missing", "cn", ""}, {"arm", "cn", "linux/amd64"}, {"", "", ""}, {"", "home", ""}} {
		if _, err := TargetPlatform(r, c.node, c.region, c.requested); err == nil {
			t.Fatal("accepted missing/mismatched target", c)
		}
	}
}
func TestBuildRepositoryInputs(t *testing.T) {
	for _, url := range []string{"https://secret@example.test/repo", "https://example.test/repo?token=secret", "ext::command", "--upload-pack=cmd", "file:///etc"} {
		if _, err := NormalizeRepository(url); err == nil {
			t.Fatal("accepted unsafe source", url)
		}
	}
	for _, url := range []string{"https://example.test/a/b.git", "git@example.test:a/b.git", "ssh://example.test/a/b.git", "example.test/a/b"} {
		if _, err := NormalizeRepository(url); err != nil {
			t.Fatal(err)
		}
	}
	for _, repo := range []string{"../escape", "Upper/Case", "repo:tag", "repo@sha256:abc"} {
		if err := ValidateImageRepository("registry.test:5000", repo); err == nil {
			t.Fatal(repo)
		}
	}
}
