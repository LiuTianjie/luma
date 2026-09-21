package controlstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"slices"
	"time"

	"github.com/LiuTianjie/luma/internal/registry"
)

func (s *Store) RegistryCredentials(ctx context.Context) ([]registry.CredentialInfo, error) {
	items := []registry.CredentialInfo{}
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		d, err := configDocument(ctx, tx, "registries", 4<<20)
		if err != nil {
			return err
		}
		for host, raw := range d {
			item, err := readDocument(raw)
			if err != nil || string(raw) == "null" {
				continue
			}
			address := item.text("serverAddress")
			if address == "" {
				address = host
			}
			items = append(items, registry.CredentialInfo{Host: host, ServerAddress: address, Username: item.text("username"), Configured: item.text("username") != "" && item.text("password") != ""})
		}
		slices.SortFunc(items, func(a, b registry.CredentialInfo) int {
			if a.Host < b.Host {
				return -1
			}
			if a.Host > b.Host {
				return 1
			}
			return 0
		})
		return nil
	})
	return items, err
}

func (s *Store) ChangeRegistryCredential(ctx context.Context, token string, change registry.CredentialChange, remove bool) (registry.CredentialResult, error) {
	c, err := registry.ValidateCredential(change, remove)
	if err != nil {
		return registry.CredentialResult{}, err
	}
	result := registry.CredentialResult{Host: c.Host}
	if s.writer == nil {
		return result, ErrReadOnly
	}
	err = s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		if err := checkManagement(ctx, tx, token); err != nil {
			return err
		}
		d, err := configDocument(ctx, tx, "registries", 4<<20)
		if err != nil {
			return err
		}
		if remove {
			_, exists := d[c.Host]
			result.Removed = &exists
			delete(d, c.Host)
		} else {
			if err := d.set(c.Host, map[string]any{"serverAddress": c.Host, "username": c.Username, "password": c.Password, "updatedAt": time.Now().Unix()}); err != nil {
				return err
			}
			saved := true
			result.Saved, result.Username = &saved, c.Username
		}
		raw, err := json.Marshal(d)
		if err != nil {
			return err
		}
		if len(raw) > 4<<20 {
			return fmt.Errorf("%w: registry catalog exceeds 4 MiB", registry.ErrCredentialInput)
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO control_config(key,payload) VALUES('registries',?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload`, string(raw)); err != nil {
			return err
		}
		return invalidateSetupCheck(ctx, tx, "registry")
	})
	return result, err
}
