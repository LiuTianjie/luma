package controlstore

import (
	"context"
	"errors"
	"github.com/LiuTianjie/luma/internal/deployment"
	"testing"
)

func TestLegacyRelayReservations(t *testing.T) {
	dir, db := fixture(t)
	if _, err := db.Exec(`INSERT INTO control_config VALUES('deployments','{"services":{"legacy":{"status":"active","manifest":"exposure: tcp-relay\nport: 5432\n"}},"compose":{"stack":{"status":"pending","tcpRelayPorts":[9000]}}}')`); err != nil {
		t.Fatal(err)
	}
	store, err := OpenReadOnly(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	snapshot, err := store.DeploymentSnapshot(context.Background(), "service", "new")
	if err != nil || len(snapshot.RelayReservations) != 2 {
		t.Fatal(snapshot, err)
	}
	for _, p := range []int{5432, 9000} {
		if err := deployment.CheckRelayPorts("service", "new", []int{p}, snapshot.RelayReservations); err == nil {
			t.Fatal("missing existing reservation", p)
		}
	}
}

func TestRelayConflictRecheckedAtBegin(t *testing.T) {
	dir, db := fixture(t)
	ctx := context.Background()
	store, err := OpenWriter(ctx, dir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	snapshot, err := store.DeploymentSnapshot(ctx, "service", "new")
	if err != nil {
		t.Fatal(err)
	}
	// A different deployment changes after preview; this slug's revision is unchanged.
	if _, err := db.Exec(`INSERT INTO control_config VALUES('deployments','{"services":{"other":{"status":"pending","tcpRelayPorts":[5432]}}}')`); err != nil {
		t.Fatal(err)
	}
	err = store.BeginDeployment(ctx, "management", snapshot.Revision, deployment.Record{Kind: "service", Slug: "new", TCPRelayPorts: []int{5432}}, nil)
	if !errors.Is(err, deployment.ErrInput) {
		t.Fatal("transaction did not reject newly claimed port", err)
	}
}
