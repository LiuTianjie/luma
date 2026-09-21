package imageexec_test

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"testing"

	"github.com/LiuTianjie/luma/internal/adapters/imageexec"
	"github.com/LiuTianjie/luma/internal/adapters/process"
	"github.com/LiuTianjie/luma/internal/node"
)

// This opt-in check uses an already-present image and rejects any pull attempt.
// It cannot download an image, start a container or modify the local daemon.
type inspectOnly struct{ runner process.Runner }

func (r inspectOnly) Run(ctx context.Context, c node.Command, emit func(string)) (node.CommandResult, error) {
	if len(c.Args) < 3 || c.Args[1] != "image" || c.Args[2] != "inspect" {
		return node.CommandResult{}, fmt.Errorf("integration probe permits only image inspect")
	}
	return r.runner.Run(ctx, c, emit)
}
func TestDockerCachedImageIntegration(t *testing.T) {
	image := os.Getenv("LUMA_TEST_DOCKER_IMAGE")
	if image == "" {
		t.Skip("set LUMA_TEST_DOCKER_IMAGE to an existing local image")
	}
	docker, err := exec.LookPath("docker")
	if err != nil {
		t.Fatal(err)
	}
	payload, _ := json.Marshal(map[string]string{"image": image})
	e := imageexec.Executor{Runner: inspectOnly{}, Docker: docker, Scratch: t.TempDir()}
	out, err := e.Execute(context.Background(), node.Task{ID: "read-only-probe", Action: "resolve-docker-image", Payload: payload}, nil)
	if err != nil {
		t.Fatal(err)
	}
	var got struct {
		Image    string
		Pulled   bool
		Deployed string
	}
	if err := json.Unmarshal(out.Result, &got); err != nil {
		t.Fatal(err)
	}
	if got.Image != image || got.Pulled || got.Deployed == "" {
		t.Fatalf("unexpected cached image result %+v", got)
	}
}
