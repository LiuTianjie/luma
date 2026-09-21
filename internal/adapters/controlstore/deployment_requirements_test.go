package controlstore

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
)

func TestPrerequisiteSnapshotContainsPresenceOnly(t *testing.T) {
	dir, db := fixture(t)
	for key, value := range map[string]string{"secrets": `{"CF":"private-cf-value","EGRESS_SUBSCRIPTION_URL":"private-egress","UNRELATED":"unrelated-private"}`, "registries": `{"private.test":{"username":"u","password":"private-registry"}}`, "setupChecks": `{"checks":{"cloudflare":{"status":"ready","detail":"private-detail"},"egress":{"status":"stale"}}}`} {
		if _, err := db.Exec(`INSERT INTO control_config VALUES(?,?)`, key, value); err != nil {
			t.Fatal(err)
		}
	}
	s, err := OpenReadOnly(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	state, err := s.DeploymentPrerequisites(context.Background(), []string{"CF", "EGRESS_SUBSCRIPTION_URL"})
	if err != nil || !state.Secrets["CF"] || !state.Registries["private.test"] || !state.Verified["cloudflare"] || state.Verified["egress"] {
		t.Fatal(state, err)
	}
	raw, _ := json.Marshal(state)
	for _, secret := range []string{"private-cf-value", "private-egress", "private-registry", "private-detail", "UNRELATED"} {
		if strings.Contains(string(raw), secret) {
			t.Fatal("leaked private prerequisite metadata")
		}
	}
}
