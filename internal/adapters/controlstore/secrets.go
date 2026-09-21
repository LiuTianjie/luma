package controlstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/LiuTianjie/luma/internal/credentials"
	"slices"
)

// GlobalSecrets projects only the explicitly named infrastructure credentials.
// They are never included in a deployment snapshot or the HTTP preview response.
func (s *Store) GlobalSecrets(ctx context.Context, names []string) (map[string]string, error) {
	result := map[string]string{}
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		d, err := configDocument(ctx, tx, "secrets", 4<<20)
		if err != nil {
			return err
		}
		for _, name := range names {
			if raw, ok := d[name]; ok && string(raw) != "null" {
				var value string
				if json.Unmarshal(raw, &value) != nil {
					return errors.New("invalid infrastructure credential value")
				}
				result[name] = value
			}
		}
		return nil
	})
	return result, err
}

func writeSecretDocument(ctx context.Context, tx *sql.Tx, key string, d document) error {
	raw, err := json.Marshal(d)
	if err != nil {
		return err
	}
	if len(raw) > 4<<20 {
		return fmt.Errorf("%w: secret catalog exceeds 4 MiB", credentials.ErrSecretInput)
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO control_config(key,payload) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload`, key, string(raw))
	return err
}
func (s *Store) SecretNames(ctx context.Context) ([]string, error) {
	result := []string{}
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		global, err := configDocument(ctx, tx, "secrets", 4<<20)
		if err != nil {
			return err
		}
		scopes, err := configDocument(ctx, tx, "scopedSecrets", 4<<20)
		if err != nil {
			return err
		}
		for name := range global {
			result = append(result, name)
		}
		for scope := range scopes {
			for name := range scopes.object(scope) {
				result = append(result, scope+"/"+name)
			}
		}
		slices.Sort(result)
		return nil
	})
	return result, err
}
func (s *Store) ChangeSecret(ctx context.Context, token string, change credentials.SecretChange, remove bool) (credentials.SecretResult, error) {
	c, err := credentials.ValidateSecretChange(change, remove)
	if err != nil {
		return credentials.SecretResult{}, err
	}
	result := credentials.SecretResult{Name: c.Name, Scope: c.Scope}
	if s.writer == nil {
		return result, ErrReadOnly
	}
	err = s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		if err := checkManagement(ctx, tx, token); err != nil {
			return err
		}
		key := "secrets"
		if c.Scope != "" {
			key = "scopedSecrets"
		}
		d, err := configDocument(ctx, tx, key, 4<<20)
		if err != nil {
			return err
		}
		values := d
		if c.Scope != "" {
			values = d.object(c.Scope)
		}
		if remove {
			_, exists := values[c.Name]
			result.Removed = &exists
			delete(values, c.Name)
		} else {
			saved := true
			result.Saved = &saved
			if err := values.set(c.Name, *c.Value); err != nil {
				return err
			}
		}
		if c.Scope != "" {
			if len(values) == 0 {
				delete(d, c.Scope)
			} else if err := d.set(c.Scope, values); err != nil {
				return err
			}
		}
		if err := writeSecretDocument(ctx, tx, key, d); err != nil {
			return err
		}
		if c.Scope == "" {
			if kind := credentials.SecretSetupKind(c.Name); kind != "" {
				return invalidateSetupCheck(ctx, tx, kind)
			}
		}
		return nil
	})
	return result, err
}
func invalidateSetupCheck(ctx context.Context, tx *sql.Tx, kind string) error {
	checks, err := configDocument(ctx, tx, "setupChecks", 4<<20)
	if err != nil {
		return err
	}
	items := checks.object("checks")
	if _, ok := items[kind]; !ok {
		return nil
	}
	item := items.object(kind)
	_ = item.set("status", "stale")
	_ = item.set("detail", "configuration changed; run setup check again")
	_ = items.set(kind, item)
	_ = checks.set("checks", items)
	return writeSecretDocument(ctx, tx, "setupChecks", checks)
}
func (s *Store) ReadDeploymentSecrets(ctx context.Context, scope string, names []string) (credentials.SecretSnapshot, error) {
	result := credentials.SecretSnapshot{Global: map[string]string{}, Scoped: map[string]string{}}
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		global, err := configDocument(ctx, tx, "secrets", 4<<20)
		if err != nil {
			return err
		}
		scopes, err := configDocument(ctx, tx, "scopedSecrets", 4<<20)
		if err != nil {
			return err
		}
		scoped := scopes.object(scope)
		result.HasScoped = len(scoped) > 0
		for _, name := range names {
			for _, pair := range []struct {
				source document
				dest   map[string]string
			}{{global, result.Global}, {scoped, result.Scoped}} {
				if raw, ok := pair.source[name]; ok && string(raw) != "null" {
					var value string
					if json.Unmarshal(raw, &value) != nil {
						return errors.New("invalid stored deployment secret")
					}
					pair.dest[name] = value
				}
			}
		}
		return nil
	})
	return result, err
}
func importDeploymentSecrets(ctx context.Context, tx *sql.Tx, scope string, imports map[string]string) error {
	if len(imports) == 0 {
		return nil
	}
	d, err := configDocument(ctx, tx, "scopedSecrets", 4<<20)
	if err != nil {
		return err
	}
	values := d.object(scope)
	for name, value := range imports {
		if !credentials.ValidSecretName(name) {
			return credentials.ErrSecretInput
		}
		if err := values.set(name, value); err != nil {
			return err
		}
	}
	if err := d.set(scope, values); err != nil {
		return err
	}
	return writeSecretDocument(ctx, tx, "scopedSecrets", d)
}
