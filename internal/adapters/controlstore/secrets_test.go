package controlstore

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/LiuTianjie/luma/internal/credentials"
	"reflect"
	"testing"
)

func TestGlobalSecretsProjectsOnlyRequestedCredentials(t *testing.T) {
	dir, db := fixture(t)
	if _, err := db.Exec(`INSERT INTO control_config(key,payload) VALUES('secrets','{"TOKEN":"one","unrelated":"private"}')`); err != nil {
		t.Fatal(err)
	}
	store, err := OpenReadOnly(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	got, err := store.GlobalSecrets(context.Background(), []string{"TOKEN", "MISSING"})
	if err != nil || len(got) != 1 || got["TOKEN"] != "one" {
		t.Fatal("wrong credential projection", err)
	}
}

func TestSecretManagementPreservesScopesAndInvalidatesProviderCheck(t *testing.T) {
	dir, db := fixture(t)
	for key, value := range map[string]string{"secrets": `{"KEEP":"unchanged"}`, "scopedSecrets": `{"other":{"TOKEN":"private"}}`, "setupChecks": `{"checks":{"cloudflare":{"status":"ready","extension":"keep"},"egress":{"status":"ready"}}}`} {
		if _, err := db.Exec(`INSERT INTO control_config(key,payload) VALUES(?,?)`, key, value); err != nil {
			t.Fatal(err)
		}
	}
	store, err := OpenWriter(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	ctx := context.Background()
	value := "new"
	_, err = store.ChangeSecret(ctx, "management", credentials.SecretChange{Name: "CLOUDFLARE_API_TOKEN", Value: &value}, false)
	if err != nil {
		t.Fatal(err)
	}
	_, err = store.ChangeSecret(ctx, "management", credentials.SecretChange{Name: "TOKEN", Scope: "My App", Value: &value}, false)
	if err != nil {
		t.Fatal(err)
	}
	names, err := store.SecretNames(ctx)
	if err != nil || !reflect.DeepEqual(names, []string{"CLOUDFLARE_API_TOKEN", "KEEP", "my-app/TOKEN", "other/TOKEN"}) {
		t.Fatal(names, err)
	}
	var raw []byte
	if err := db.QueryRow(`SELECT payload FROM control_config WHERE key='setupChecks'`).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var checks map[string]map[string]map[string]string
	json.Unmarshal(raw, &checks)
	if checks["checks"]["cloudflare"]["status"] != "stale" || checks["checks"]["cloudflare"]["extension"] != "keep" || checks["checks"]["egress"]["status"] != "ready" {
		t.Fatal(checks)
	}
	if _, err := db.Exec(`UPDATE control_config SET payload='"rotated"' WHERE key='deployToken'`); err != nil {
		t.Fatal(err)
	}
	if _, err := store.ChangeSecret(ctx, "management", credentials.SecretChange{Name: "TOKEN", Scope: "my-app"}, true); !errors.Is(err, credentials.ErrUnauthorized) {
		t.Fatal(err)
	}
	result, err := store.ChangeSecret(ctx, "rotated", credentials.SecretChange{Name: "TOKEN", Scope: "my-app"}, true)
	if err != nil || result.Removed == nil || !*result.Removed {
		t.Fatal(result, err)
	}
	snapshot, err := store.ReadDeploymentSecrets(ctx, "other", []string{"TOKEN"})
	if err != nil || snapshot.Scoped["TOKEN"] != "private" {
		t.Fatal("other scope changed", err)
	}
}
