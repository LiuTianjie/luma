package controlstore

import (
	"context"
	"database/sql"

	"github.com/LiuTianjie/luma/internal/deployment"
)

func (s *Store) DeploymentPrerequisites(ctx context.Context, names []string) (deployment.PrerequisiteState, error) {
	result := deployment.PrerequisiteState{Secrets: map[string]bool{}, Registries: map[string]bool{}, Verified: map[string]bool{}}
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		secrets, err := configDocument(ctx, tx, "secrets", 4<<20)
		if err != nil {
			return err
		}
		for _, name := range names {
			result.Secrets[name] = secrets.text(name) != ""
		}
		catalog, err := configDocument(ctx, tx, "registries", 4<<20)
		if err != nil {
			return err
		}
		for host := range catalog {
			entry := catalog.object(host)
			result.Registries[host] = entry.text("username") != "" && entry.text("password") != ""
		}
		checks, err := configDocument(ctx, tx, "setupChecks", 4<<20)
		if err != nil {
			return err
		}
		for _, kind := range []string{"cloudflare", "egress"} {
			result.Verified[kind] = checks.object("checks").object(kind).text("status") == "ready"
		}
		return nil
	})
	return result, err
}
