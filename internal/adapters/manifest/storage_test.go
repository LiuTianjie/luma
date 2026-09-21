package manifest_test

import (
	"strings"
	"testing"

	"github.com/LiuTianjie/luma/internal/adapters/manifest"
	"github.com/LiuTianjie/luma/internal/adapters/nomad"
)

func TestLocalStorageFollowsDeploymentNode(t *testing.T) {
	for _, localNode := range []string{"", "worker-a"} {
		sidecar := "name: app\nregion: cn\nservices:\n  db:\n    node: worker-a\nvolumes:\n  data:\n    local:\n      path: /srv/luma/app/data\n"
		if localNode != "" {
			sidecar += "      node: " + localNode + "\n"
		}
		body := []byte("services:\n  db:\n    image: postgres:16\n    volumes: [data:/var/lib/postgresql/data]\n")
		c, err := manifest.DecodeCompose([]byte(sidecar), body)
		if err != nil {
			t.Fatal(err)
		}
		placement, err := c.Placement()
		if err != nil || placement.Node != "worker-a" {
			t.Fatalf("%+v %v", placement, err)
		}
		plan, err := (nomad.Compiler{}).CompileCompose(c, manifest.DefaultConfig())
		if err != nil {
			t.Fatal(err)
		}
		job := plan.Artifacts[0].Content
		if !strings.Contains(job, "/srv/luma/app/data") || !strings.Contains(job, "worker-a") || strings.Contains(job, "driver_config") || strings.Contains(job, "nfs") {
			t.Fatal(job)
		}
		c.Services[0].Node = "worker-b"
		if localNode != "" {
			if _, err := c.Placement(); err == nil {
				t.Fatal("allowed storage and service on different nodes")
			}
		}
	}
}

func TestNativeLegacyNFSMetadataFailsExplicitly(t *testing.T) {
	_, err := manifest.DecodeService([]byte("name: db\nimage: postgres:16\nregion: cn\nvolumes: [data:/db]\nstorage:\n  data:\n    storageClass: old-nfs\n"))
	if err == nil || !strings.Contains(err.Error(), "removed") {
		t.Fatalf("legacy NFS silently accepted: %v", err)
	}
}
