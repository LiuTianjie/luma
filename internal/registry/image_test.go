package registry

import (
	"strings"
	"testing"
)

func TestImageIdentityAndMatchingDigest(t *testing.T) {
	for _, ref := range []string{"nginx:alpine", "library/nginx", "ghcr.io/org/app:v1", "localhost:5000/app:v1", "[::1]:5000/app:v1", "repo.test/app@sha256:" + strings.Repeat("a", 64)} {
		if _, err := ParseImage(ref); err != nil {
			t.Errorf("%s: %v", ref, err)
		}
	}
	for _, ref := range []string{"", "--help", "https://repo.test/app", "a/b\n--flag", "a/../b", "a//b", "x@y", "repo.test/app@sha256:short", "repo.test/app:"} {
		if _, err := ParseImage(ref); err == nil {
			t.Errorf("accepted invalid reference %q", ref)
		}
	}
	i, _ := ParseImage("repo.test/app:v1")
	want := "repo.test/app@sha256:" + strings.Repeat("a", 64)
	other := "other.test/app@sha256:" + strings.Repeat("b", 64)
	if got := RepoDigest(i, `["`+other+`","`+want+`"]`); got != want {
		t.Fatalf("digest = %s", got)
	}
	if got := RepoDigest(i, `["token@sha256:not-a-digest"]`); got != "" {
		t.Fatalf("trusted invalid digest %s", got)
	}
}
func TestMirrorDestinationPolicy(t *testing.T) {
	for _, item := range []struct{ action, path string }{{"mirror-control-image", "luma-control"}, {"mirror-system-image", "luma-system/traefik"}, {"cache-runtime-image", "luma-cache/app"}} {
		p, err := PlanMirror(item.action, "ghcr.io/example/app:v1", "127.0.0.1:5000/"+item.path+":v1", "registry.test/"+item.path+":v1", "")
		if err != nil || p.Kind == "" {
			t.Fatalf("plan: %+v %v", p, err)
		}
	}
	for _, pair := range [][2]string{
		{"local.test/luma-control:v1", "registry.test/luma-control:v2"},
		{"local.test/luma-system/a:v1", "registry.test/luma-system/b:v1"},
		{"local.test/other/app:v1", "registry.test/other/app:v1"},
		{"luma-system/app:v1", "luma-system/app:v1"},
		{"local.test/luma-system/app", "registry.test/luma-system/app"},
	} {
		if _, err := PlanMirror("mirror-system-image", "nginx", pair[0], pair[1], ""); err == nil {
			t.Fatalf("accepted mismatched destination %v", pair)
		}
	}
}
