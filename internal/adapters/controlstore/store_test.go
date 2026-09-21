package controlstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/LiuTianjie/luma/internal/history"
)

func fixture(t *testing.T) (string, *sql.DB) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, databaseName)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	f.Close()
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	for _, statement := range []string{
		`PRAGMA journal_mode=WAL`,
		`CREATE TABLE database_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)`,
		`INSERT INTO database_meta VALUES('schema_version','1'),('state_initialized','1'),('database_id','fixture')`,
		`CREATE TABLE control_config(key TEXT PRIMARY KEY,payload TEXT NOT NULL)`,
		`INSERT INTO control_config VALUES('deployToken','"management"'),('clusterId','"cluster"'),('domain','"example.test"')`,
		`CREATE TABLE control_entities(kind TEXT,id TEXT,payload TEXT,status TEXT,app TEXT,created_at INTEGER,updated_at INTEGER,ordinal INTEGER,PRIMARY KEY(kind,id))`,
		`CREATE TABLE control_events(kind TEXT,entity_id TEXT,stream TEXT,position INTEGER,payload TEXT,PRIMARY KEY(kind,entity_id,stream,position))`,
	} {
		if _, err := db.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(dir, authorityName), []byte(`{"databaseId":"fixture","schemaVersion":1}`), 0600); err != nil {
		t.Fatal(err)
	}
	return dir, db
}

func TestReadOnlyConnectionsIncludeWALAndDenyWrites(t *testing.T) {
	dir, writer := fixture(t)
	ctx := context.Background()
	store, err := OpenReadOnly(ctx, dir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	connections := []*sql.Conn{}
	defer func() {
		for _, conn := range connections {
			conn.Close()
		}
	}()
	// All pooled connections inherit read-only settings, not just the first one.
	for range 4 {
		conn, err := store.db.Conn(ctx)
		if err != nil {
			t.Fatal(err)
		}
		connections = append(connections, conn)
		if _, err := conn.ExecContext(ctx, `UPDATE control_config SET payload='"overwritten"' WHERE key='deployToken'`); err == nil {
			t.Fatal("read-only connection wrote state")
		}
	}
	for _, conn := range connections {
		conn.Close()
	}
	connections = nil
	if _, err := writer.Exec(`UPDATE control_config SET payload='"rotated"' WHERE key='deployToken'`); err != nil {
		t.Fatal(err)
	}
	identity, err := store.ManagementIdentity(ctx)
	if err != nil || identity.Token != "rotated" {
		t.Fatalf("WAL changes not visible: %+v %v", identity, err)
	}
	if _, err := writer.Exec(`INSERT INTO control_entities VALUES('deploymentEvents','a','{"id":"a"}','succeeded','app',1,1,0)`); err != nil {
		t.Fatal(err)
	}
	q, err := history.ParseList(nil, "", false)
	if err != nil {
		t.Fatal(err)
	}
	result, err := store.List(ctx, q)
	if err != nil || len(result.Rows) != 1 {
		t.Fatalf("history WAL read: %+v %v", result, err)
	}
	if !json.Valid(result.Rows[0].Payload) {
		t.Fatal("invalid payload conversion")
	}
}

func TestAuthorityAndDatabaseReplacementAreRejectedOnEachRead(t *testing.T) {
	dir, _ := fixture(t)
	ctx := context.Background()
	store, err := OpenReadOnly(ctx, dir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	marker := filepath.Join(dir, authorityName)
	if err := os.WriteFile(marker, []byte(`{"databaseId":"other","schemaVersion":1}`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := store.ManagementIdentity(ctx); err == nil {
		t.Fatal("accepted changed authority")
	}
	if err := os.WriteFile(marker, []byte(`{"databaseId":"fixture","schemaVersion":1}`), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(filepath.Join(dir, databaseName), filepath.Join(dir, "old.sqlite3")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, databaseName), nil, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := store.ManagementIdentity(ctx); err == nil {
		t.Fatal("accepted replaced database")
	}
}

func TestMissingStateIsNeverInitialized(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "absent")
	if _, err := OpenReadOnly(context.Background(), dir); err == nil {
		t.Fatal("accepted missing directory")
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("created missing directory: %v", err)
	}
	dir = t.TempDir()
	if _, err := OpenReadOnly(context.Background(), dir); err == nil {
		t.Fatal("accepted missing database")
	}
	entries, err := os.ReadDir(dir)
	if err != nil || len(entries) != 0 {
		t.Fatalf("created state: %v %v", entries, err)
	}
}
