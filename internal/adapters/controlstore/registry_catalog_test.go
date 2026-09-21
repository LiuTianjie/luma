package controlstore

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/registry"
)

func TestRegistryRotationPersistsOnlyAuthorizedChanges(t *testing.T) {
	dir, db := fixture(t)
	ctx := context.Background()
	s, err := OpenWriter(ctx, dir)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	change := registry.CredentialChange{Host: "DOCKER.IO/", Username: "user", Password: "registry-private-value"}
	result, err := s.ChangeRegistryCredential(ctx, "management", change, false)
	if err != nil || result.Host != "docker.io" {
		t.Fatal(result, err)
	}
	items, err := s.RegistryCredentials(ctx)
	if err != nil || len(items) != 1 || !items[0].Configured {
		t.Fatal(items, err)
	}
	raw, _ := json.Marshal([]any{result, items})
	if strings.Contains(string(raw), change.Password) {
		t.Fatal("public metadata leaked a password")
	}
	imageAuth, err := s.DeploymentImageAuth(ctx, map[string]string{"app": "busybox:1"})
	if err != nil || imageAuth["app"].Password != change.Password || imageAuth["app"].ServerAddress != "registry-1.docker.io" {
		t.Fatal("Docker Hub alias lookup failed", err)
	}
	if _, err := db.Exec(`UPDATE control_config SET payload='"rotated"' WHERE key='deployToken'`); err != nil {
		t.Fatal(err)
	}
	change.Password = "replacement"
	for _, remove := range []bool{false, true} {
		if _, err := s.ChangeRegistryCredential(ctx, "management", change, remove); !errors.Is(err, credentials.ErrUnauthorized) {
			t.Fatal(err)
		}
	}
	imageAuth, err = s.DeploymentImageAuth(ctx, map[string]string{"app": "busybox:1"})
	if err != nil || imageAuth["app"].Password != "registry-private-value" {
		t.Fatal("unauthorized update changed credential", err)
	}
	if _, err := s.ChangeRegistryCredential(ctx, "rotated", change, true); err != nil {
		t.Fatal(err)
	}
	imageAuth, err = s.DeploymentImageAuth(ctx, map[string]string{"app": "busybox:1"})
	if err != nil || len(imageAuth) != 0 {
		t.Fatal("removed credential remained usable", err)
	}
}
