package controlstore

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
)

func TestDeploymentGatewaySnapshotContainsOnlyRoutingMetadata(t *testing.T) {
	dir, db := fixture(t)
	for key, value := range map[string]string{"regions": `{"private":{"egress":"proxy"},"abroad":{"egress":"direct"}}`, "nomadRpcAddr": `"100.64.0.1:4647"`, "nomadServerAddr": `"100.64.0.2:4647"`, "nomadAddr": `"http://100.64.0.3:4646"`, "secrets": `{"PRIVATE":"never-project-this"}`} {
		if _, err := db.Exec(`INSERT INTO control_config VALUES(?,?)`, key, value); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := db.Exec(`INSERT INTO control_entities(kind,id,payload) VALUES('nodes','manager','{"roles":["nomad-manager"],"tailscaleIP":"100.64.0.4","agent":{"tokenHash":"never-project-this"}}')`); err != nil {
		t.Fatal(err)
	}
	store, err := OpenReadOnly(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	snapshot, err := store.DeploymentSnapshot(context.Background(), "service", "app")
	if err != nil {
		t.Fatal(err)
	}
	s := snapshot.Egress
	if s.NomadRPCAddr != "100.64.0.1:4647" || s.NomadServerAddr != "100.64.0.2:4647" || s.NomadHTTPAddr != "http://100.64.0.3:4646" || s.Regions["private"] != "proxy" || len(s.ManagerHosts) != 1 || s.ManagerHosts[0] != "100.64.0.4" {
		t.Fatal(s)
	}
	encoded, err := json.Marshal(snapshot)
	if err != nil || strings.Contains(string(encoded), "never-project-this") {
		t.Fatal("snapshot contains a credential", err)
	}
}
