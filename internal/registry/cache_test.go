package registry

import (
	"crypto/sha256"
	"fmt"
	"strings"
	"testing"
)

func TestCacheIdentityAndInternalReferences(t *testing.T) {
	insecure := true
	config := CacheConfig{PullHost: "registry.internal:5000", PushHost: "127.0.0.1:5000", Builder: "builder", Insecure: &insecure}
	p, err := PlanCache("busybox:1.36", "linux/arm64", config)
	hash := fmt.Sprintf("%x", sha256.Sum256([]byte("busybox:1.36\nlinux/arm64")))[:20]
	if err != nil || !p.Needed || p.Push != "127.0.0.1:5000/luma-cache/registry-1.docker.io/library/busybox:"+hash {
		t.Fatal(p, err)
	}
	if _, err := PlanMirror("cache-runtime-image", p.Source, p.Push, p.Destination, p.Platform); err != nil {
		t.Fatal(err)
	}
	internal, err := PlanCache("127.0.0.1:5000/app:v1", "", config)
	if err != nil || internal.Needed || internal.Deployed != "registry.internal:5000/app:v1" {
		t.Fatal(internal, err)
	}
	digest := "sha256:" + strings.Repeat("a", 64)
	internal, err = PlanCache("registry.internal:5000/app@"+digest, "", config)
	if err != nil || internal.Deployed != "registry.internal:5000/app@"+digest {
		t.Fatal(internal, err)
	}
	for _, c := range []CacheConfig{{PullHost: "registry.internal:5000"}, {PullHost: config.PullHost, PushHost: config.PushHost}, {PullHost: "http://bad", PushHost: config.PushHost, Insecure: &insecure}} {
		if _, err := PlanCache("busybox:1.36", "", c); err == nil {
			t.Fatal("accepted incomplete or invalid cache configuration", c)
		}
	}
	if _, err := PlanCache("busybox:1.36", "windows/amd64", config); err == nil {
		t.Fatal("accepted unsupported platform")
	}
	legacy, err := PlanCache("busybox:1.36", "", CacheConfig{})
	if err != nil || legacy.Needed || legacy.Deployed != "busybox:1.36" {
		t.Fatal(legacy, err)
	}
}
