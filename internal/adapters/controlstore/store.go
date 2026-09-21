// Package controlstore implements bounded repositories over the existing
// Manager SQLite schema. The initial Control slice is strictly read-only.
package controlstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"

	"github.com/LiuTianjie/luma/internal/credentials"
	_ "modernc.org/sqlite"
)

const databaseName = "control.sqlite3"
const authorityName = "control-sqlite-authority.json"

type Store struct {
	db         *sql.DB
	writer     *sql.DB
	unlock     func()
	root       *os.Root
	file       os.FileInfo
	databaseID string
}

// OpenReadOnly requires an already initialized recovery directory and authority
// marker. It never initializes a DB, upgrades its schema, or imports control.json.
func OpenReadOnly(ctx context.Context, directory string) (_ *Store, err error) {
	absolute, err := filepath.Abs(directory)
	if err != nil {
		return nil, err
	}
	root, err := os.OpenRoot(absolute)
	if err != nil {
		return nil, fmt.Errorf("open Control state directory: %w", err)
	}
	s := &Store{root: root}
	defer func() {
		if err != nil {
			s.Close()
		}
	}()
	if s.file, err = privateFile(root, databaseName); err != nil {
		return nil, err
	}
	if s.databaseID, err = s.authority(); err != nil {
		return nil, err
	}
	u := url.URL{Scheme: "file", Path: filepath.Join(absolute, databaseName)}
	q := url.Values{"mode": {"ro"}, "_pragma": {"query_only(1)", "busy_timeout(2000)", "trusted_schema(0)", "foreign_keys(1)"}}
	u.RawQuery = q.Encode()
	if s.db, err = sql.Open("sqlite", u.String()); err != nil {
		return nil, fmt.Errorf("open Control SQLite: %w", err)
	}
	s.db.SetMaxOpenConns(4)
	s.db.SetMaxIdleConns(4)
	err = s.snapshot(ctx, func(tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, "PRAGMA integrity_check")
		if err != nil {
			return err
		}
		defer rows.Close()
		count := 0
		for rows.Next() {
			var result string
			if err := rows.Scan(&result); err != nil {
				return err
			}
			if result != "ok" {
				return errors.New("Control database integrity check failed")
			}
			count++
		}
		if err := rows.Err(); err != nil {
			return err
		}
		if count != 1 {
			return errors.New("Control database integrity check failed")
		}
		violations, err := tx.QueryContext(ctx, "PRAGMA foreign_key_check")
		if err != nil {
			return err
		}
		bad := violations.Next()
		readErr := violations.Err()
		violations.Close()
		if readErr != nil {
			return readErr
		}
		if bad {
			return errors.New("Control database has foreign key violations")
		}
		for _, table := range []string{"control_config", "control_entities", "control_events"} {
			var invalid int
			if err := tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM "+table+" WHERE NOT json_valid(payload)").Scan(&invalid); err != nil {
				return err
			}
			if invalid != 0 {
				return errors.New("Control database contains invalid JSON")
			}
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("verify restored Control database: %w", err)
	}
	return s, nil
}

func privateFile(root *os.Root, name string) (os.FileInfo, error) {
	info, err := root.Lstat(name)
	if err != nil {
		return nil, fmt.Errorf("required Control state file %s: %w", name, err)
	}
	if !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		return nil, fmt.Errorf("Control state file %s must be a private regular file (mode 0600)", name)
	}
	return info, nil
}
func (s *Store) authority() (string, error) {
	if _, err := privateFile(s.root, authorityName); err != nil {
		return "", err
	}
	f, err := s.root.Open(authorityName)
	if err != nil {
		return "", err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, 4097))
	if err != nil {
		return "", err
	}
	var marker struct {
		DatabaseID    string `json:"databaseId"`
		SchemaVersion int    `json:"schemaVersion"`
	}
	if len(data) > 4096 || json.Unmarshal(data, &marker) != nil || marker.DatabaseID == "" || marker.SchemaVersion != 1 {
		return "", errors.New("invalid Control database authority marker; restore a verified backup")
	}
	return marker.DatabaseID, nil
}

func (s *Store) snapshot(ctx context.Context, read func(*sql.Tx) error) error {
	return s.transaction(ctx, s.db, true, read)
}

func (s *Store) transaction(ctx context.Context, db *sql.DB, readOnly bool, work func(*sql.Tx) error) error {
	info, err := privateFile(s.root, databaseName)
	if err != nil {
		return err
	}
	if !os.SameFile(info, s.file) {
		return errors.New("Control database was replaced; restart against a verified backup")
	}
	identity, err := s.authority()
	if err != nil {
		return err
	}
	if identity != s.databaseID {
		return errors.New("Control database authority changed; restore a verified backup")
	}
	tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: readOnly})
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var schema, initialized, id string
	err = tx.QueryRowContext(ctx, `SELECT
	 (SELECT value FROM database_meta WHERE key='schema_version'),
	 (SELECT value FROM database_meta WHERE key='state_initialized'),
	 (SELECT value FROM database_meta WHERE key='database_id')`).Scan(&schema, &initialized, &id)
	if err != nil {
		return fmt.Errorf("Control database metadata is missing or invalid: %w", err)
	}
	if schema != "1" {
		return errors.New("unsupported Control database schema; no automatic migration is permitted")
	}
	if initialized != "1" || id != s.databaseID {
		return errors.New("Control database identity does not match initialized Manager state; restore a verified backup")
	}
	if err := work(tx); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Store) Close() error {
	var dbErr, rootErr, writerErr error
	if s.writer != nil {
		writerErr = s.writer.Close()
	}
	if s.db != nil {
		dbErr = s.db.Close()
	}
	if s.root != nil {
		rootErr = s.root.Close()
	}
	if s.unlock != nil {
		s.unlock()
		s.unlock = nil
	}
	return errors.Join(dbErr, rootErr, writerErr)
}

func (s *Store) ManagementIdentity(ctx context.Context) (credentials.ManagementIdentity, error) {
	var identity credentials.ManagementIdentity
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, "SELECT key,payload FROM control_config WHERE key IN ('deployToken','clusterId','domain')")
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var key, payload, value string
			if err := rows.Scan(&key, &payload); err != nil {
				return err
			}
			if json.Unmarshal([]byte(payload), &value) != nil {
				return errors.New("invalid Management identity configuration")
			}
			switch key {
			case "deployToken":
				identity.Token = value
			case "clusterId":
				identity.ClusterID = value
			case "domain":
				identity.Endpoint = value
			}
		}
		if err := rows.Err(); err != nil {
			return err
		}
		if identity.ClusterID == "" || identity.Token == "" {
			return errors.New("Management identity configuration is incomplete")
		}
		return nil
	})
	return identity, err
}
