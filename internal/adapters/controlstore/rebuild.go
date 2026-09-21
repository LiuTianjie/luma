package controlstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/deployment"
)

func (s *Store) RebuildRequest(ctx context.Context, name string) (builddomain.ControlRequest, error) {
	var request builddomain.ControlRequest
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		d, err := configDocument(ctx, tx, "deployments", 32<<20)
		if err != nil {
			return err
		}
		slug := (deployment.Service{Name: name}).Slug()
		raw := d.object("services")[slug]
		other := d.object("compose")[slug]
		if len(raw) != 0 && len(other) != 0 {
			return deployment.ErrConflict
		}
		if len(raw) == 0 {
			raw = other
		}
		if len(raw) == 0 {
			return builddomain.ErrNotFound
		}
		var r deployment.Record
		if json.Unmarshal(raw, &r) != nil {
			return builddomain.ErrState
		}
		source := r.GitSource
		if source == nil || source.RepoURL == "" && (source.ProviderID == "" || source.Repository == "") {
			return fmt.Errorf("%w: application was not deployed from Git; update its manifest instead", builddomain.ErrInput)
		}
		request = builddomain.ControlRequest{RepoURL: source.RepoURL, ProviderID: source.ProviderID, Repository: source.Repository, Ref: source.Ref, BuildNode: source.BuildNode, Context: source.Context, Dockerfile: source.Dockerfile, Platform: source.Platform, RegistryHost: source.RegistryHost, PushHost: source.PushHost, Manifest: source.Manifest, ComposeContent: source.ComposeContent, ComposeSidecar: source.ComposeSidecar, ProxyMode: source.ProxyMode, ExpectedName: r.Name, Region: source.Region, Exposure: source.Exposure, Domain: source.Domain, Port: source.Port, Origin: r.Origin}
		return nil
	})
	return request, err
}
