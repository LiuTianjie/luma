package deployment

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/LiuTianjie/luma/internal/node"
	"github.com/LiuTianjie/luma/internal/registry"
)

type imageTaskFunc func(context.Context, string, node.TaskOwner, string, string, string, json.RawMessage) (json.RawMessage, error)

func (f imageTaskFunc) Run(ctx context.Context, token string, owner node.TaskOwner, n, action, cap string, payload json.RawMessage) (json.RawMessage, error) {
	return f(ctx, token, owner, n, action, cap, payload)
}

type imageResolveFunc func(context.Context, string, registry.RuntimeAuth) (string, error)

func (f imageResolveFunc) Resolve(ctx context.Context, image string, auth registry.RuntimeAuth) (string, error) {
	return f(ctx, image, auth)
}

func TestDirectImageTargetsAndMirrorFallback(t *testing.T) {
	p := Prepared{Record: Record{Kind: "service", Slug: "app", OperationID: "deploy-proof"}, Snapshot: Snapshot{ImageNodes: map[string]ImageNode{"worker": {Ready: true, Platform: "linux/arm64", Capabilities: []string{"docker-image"}}}}, RenderConfig: RenderConfig{ImageMirrors: []string{"mirror.test"}}}
	calls := 0
	digest := "sha256:" + strings.Repeat("a", 64)
	c := Coordinator{NodeTasks: imageTaskFunc(func(_ context.Context, token string, owner node.TaskOwner, n, action, cap string, raw json.RawMessage) (json.RawMessage, error) {
		calls++
		if token != "management" || owner.OperationID != p.Record.OperationID || n != "worker" || action != "resolve-docker-image" || cap != "docker-image" {
			t.Fatal("wrong task owner or executor")
		}
		var payload map[string]any
		_ = json.Unmarshal(raw, &payload)
		if payload["registryAuth"] != nil || payload["platform"] != "linux/arm64" || payload["forcePull"] != true {
			t.Fatal(payload)
		}
		if calls == 1 {
			return nil, fmt.Errorf("source unavailable")
		}
		if payload["image"] != "mirror.test/busybox:latest" {
			t.Fatal(payload)
		}
		return json.Marshal(map[string]string{"image": payload["image"].(string), "deployed": "mirror.test/busybox@" + digest, "platform": "linux/arm64"})
	})}
	r, err := c.directImage(context.Background(), "management", &p, Service{Image: "busybox:latest", Node: "worker"})
	if err != nil || calls != 2 || r.Deployed != "mirror.test/busybox@"+digest || !r.Fallback || !r.ForcePull || r.ResolvedBy != "target-node" {
		t.Fatal(r, err, calls)
	}
}

func TestUnpinnedLatestUsesRegistryWhileVersionedImageIsDeferred(t *testing.T) {
	calls := 0
	digest := "sha256:" + strings.Repeat("b", 64)
	c := Coordinator{ImageResolver: imageResolveFunc(func(_ context.Context, image string, _ registry.RuntimeAuth) (string, error) {
		calls++
		return "docker.io/library/busybox@" + digest, nil
	})}
	for _, image := range []string{"busybox", "busybox:latest"} {
		r, err := c.directImage(context.Background(), "management", &Prepared{}, Service{Image: image})
		if err != nil || r.ResolvedBy != "registry" || r.Deployed != "busybox@"+digest || r.Deferred {
			t.Fatal(r, err)
		}
	}
	r, err := c.directImage(context.Background(), "management", &Prepared{}, Service{Image: "busybox:1.36"})
	if err != nil || !r.Deferred || calls != 2 {
		t.Fatal(r, err, calls)
	}
	c.ImageResolver = imageResolveFunc(func(context.Context, string, registry.RuntimeAuth) (string, error) {
		return "attacker.test/app@" + digest, nil
	})
	if _, err := c.directImage(context.Background(), "management", &Prepared{}, Service{Image: "busybox"}); err == nil {
		t.Fatal("accepted unrelated repository")
	}
}

func TestMutableTargetMustReturnAContentDigest(t *testing.T) {
	p := Prepared{Snapshot: Snapshot{ImageNodes: map[string]ImageNode{"worker": {Ready: true, Capabilities: []string{"docker-image"}}}}}
	c := Coordinator{NodeTasks: imageTaskFunc(func(context.Context, string, node.TaskOwner, string, string, string, json.RawMessage) (json.RawMessage, error) {
		return json.RawMessage(`{"image":"busybox:latest","deployed":"busybox:latest","platform":""}`), nil
	})}
	if _, err := c.directImage(context.Background(), "management", &p, Service{Image: "busybox:latest", Node: "worker"}); err == nil {
		t.Fatal("mutable image stayed a tag after target pull")
	}
}
