package controlstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"time"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/registry"
)

func (s *Store) PlanLocalBuild(ctx context.Context, input builddomain.LocalPrepareRequest, config deployment.RenderConfig) (builddomain.Planned, error) {
	var plan builddomain.Planned
	r := input.ControlRequest
	if repositoryPart.MatchString(r.RepoURL) {
		r.RepoURL = "https://github.com/" + r.RepoURL
	}
	var err error
	r.RepoURL, err = builddomain.NormalizeRepository(r.RepoURL)
	if err != nil {
		return plan, fmt.Errorf("%w: %s", builddomain.ErrInput, err)
	}
	if len(r.SourceRevision) > 128 || strings.ContainsAny(r.SourceRevision, "\x00\r\n") || len(input.TargetNodes)+len(input.TargetRegions) > 1000 {
		return plan, builddomain.ErrInput
	}
	if r.BuildTimeout == 0 {
		r.BuildTimeout = 7200
	}
	if r.BuildTimeout < 1 || r.BuildTimeout > 86400 {
		return plan, builddomain.ErrInput
	}
	err = s.snapshot(ctx, func(tx *sql.Tx) error {
		state, _, err := deploymentState(ctx, tx, "service", "")
		if err != nil {
			return err
		}
		r.RegistryHost = state.Cache.PullHost
		host, err := registry.ValidateCredential(registry.CredentialChange{Host: r.RegistryHost}, true)
		if err != nil {
			return fmt.Errorf("%w: configure a registry host before local builds", builddomain.ErrInput)
		}
		r.RegistryHost = host.Host
		project := repositoryProject(r.RepoURL)
		if err := builddomain.ValidateImageRepository(r.RegistryHost, project); err != nil {
			return fmt.Errorf("%w: configure a registry host before local builds", builddomain.ErrInput)
		}
		image := builddomain.ImageRequest{RepoURL: r.RepoURL, Repo: project, RegistryHost: r.RegistryHost, PushHost: r.RegistryHost, TargetPlatformsByNode: map[string]string{}, TargetPlatformsByRegion: map[string][]string{}}
		for _, n := range state.ImageNodes {
			if !n.Ready || n.Platform == "" {
				continue
			}
			for _, name := range append([]string{n.Name}, n.Aliases...) {
				if name != "" {
					image.TargetPlatformsByNode[name] = n.Platform
				}
			}
			if !slices.Contains(image.TargetPlatformsByRegion[n.Region], n.Platform) {
				image.TargetPlatformsByRegion[n.Region] = append(image.TargetPlatformsByRegion[n.Region], n.Platform)
			}
		}
		nodes := slices.Clone(input.TargetNodes)
		regions := slices.Clone(input.TargetRegions)
		if input.TargetNode != "" {
			nodes = append(nodes, input.TargetNode)
		} else if input.TargetRegion != "" {
			regions = append(regions, input.TargetRegion)
		}
		if len(nodes) == 0 && len(regions) == 0 && r.Region != "" {
			regions = append(regions, r.Region)
		}
		if len(nodes)+len(regions) > 0 && len(image.TargetPlatformsByNode) == 0 {
			return fmt.Errorf("%w: no ready local deployment target", builddomain.ErrInput)
		}
		platforms := []string{}
		add := func(node, region string) error {
			value, err := builddomain.TargetPlatform(image, node, region, r.Platform)
			if err != nil {
				return fmt.Errorf("%w: %s", builddomain.ErrInput, err)
			}
			for _, p := range strings.Split(value, ",") {
				if !slices.Contains(platforms, p) {
					platforms = append(platforms, p)
				}
			}
			return nil
		}
		for _, name := range nodes {
			canonical := name
			if _, ok := state.ImageNodes[name]; !ok {
				canonical = ""
				for key, n := range state.ImageNodes {
					if slices.Contains(n.Aliases, name) {
						if canonical != "" {
							return fmt.Errorf("%w: ambiguous local target node", builddomain.ErrInput)
						}
						canonical = key
					}
				}
				if canonical == "" {
					return fmt.Errorf("%w: unknown local target node", builddomain.ErrInput)
				}
			}
			if err := add(canonical, ""); err != nil {
				return err
			}
		}
		for _, region := range regions {
			if err := add("", region); err != nil {
				return err
			}
		}
		if len(platforms) == 0 {
			if r.Platform == "" {
				return fmt.Errorf("%w: local build needs a deployment target or explicit platform", builddomain.ErrInput)
			}
			value, err := builddomain.TargetPlatform(builddomain.ImageRequest{}, "", "", r.Platform)
			if err != nil {
				return fmt.Errorf("%w: %s", builddomain.ErrInput, err)
			}
			platforms = strings.Split(value, ",")
		}
		slices.Sort(platforms)
		r.Platform = strings.Join(platforms, ",")
		r.BuildNode = state.Cache.Builder // future Git updates use the configured builder, not the local machine.
		r.PushHost = r.RegistryHost
		r.Queue = true
		r.Origin = "cli"
		r.Proxy = nil
		r.ProxyMode = ""
		r.ProviderID = ""
		r.Repository = ""
		r.Manifest = ""
		r.ComposeContent = ""
		plan = builddomain.Planned{Request: r, Image: image, ProjectKey: project}
		return nil
	})
	return plan, err
}
func (s *Store) ReserveLocalBuild(ctx context.Context, token string, submission builddomain.Submission) (builddomain.Run, error) {
	r := submission.Run
	if s.writer == nil {
		return r, ErrReadOnly
	}
	err := s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		if err := checkManagement(ctx, tx, token); err != nil {
			return err
		}
		if r.ID == "" || r.Source == "" || r.ProjectKey == "" || submission.Request.RegistryHost == "" {
			return builddomain.ErrInput
		}
		r.Mode = "local"
		r.Status = "running"
		r.GoBuild = true
		r.QueueManaged = false
		r.BuildNode = "local"
		r.Request = submission.Request.Public()
		r.CreatedAt = time.Now().Unix()
		r.UpdatedAt = r.CreatedAt
		r.ExpiresAt = r.CreatedAt + int64(max(7200, submission.Request.BuildTimeout)) + 300
		r.Message = "Waiting for local image upload"
		queue, err := privateBuildQueue(ctx, tx)
		if err != nil {
			return err
		}
		if len(queue) >= 10000 {
			return fmt.Errorf("too many active builds")
		}
		if err := queue.set(r.ID, map[string]any{"kind": "local-upload", "body": submission.Request, "goBuild": true}); err != nil {
			return err
		}
		if err := writeBuildQueue(ctx, tx, queue); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT OR IGNORE INTO control_collections(kind,shape) VALUES('buildRuns','dict')`); err != nil {
			return err
		}
		raw, err := json.Marshal(r)
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO control_entities(kind,id,payload,status,app,created_at,updated_at) VALUES('buildRuns',?,?,?,?,?,?)`, r.ID, string(raw), r.Status, "", r.CreatedAt, r.UpdatedAt)
		return err
	})
	return r, err
}
func (s *Store) CompleteLocalBuild(ctx context.Context, token, id string, complete builddomain.LocalCompleteRequest) (builddomain.Run, error) {
	var r builddomain.Run
	if s.writer == nil {
		return r, ErrReadOnly
	}
	err := s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		if err := checkManagement(ctx, tx, token); err != nil {
			return err
		}
		var err error
		r, err = readBuild(ctx, tx, id)
		if err != nil {
			return err
		}
		if !r.GoBuild || r.Mode != "local" || r.Status != "running" || r.QueueManaged || r.ExpiresAt <= time.Now().Unix() {
			return builddomain.ErrState
		}
		if err := builddomain.ValidateLocalResult(r, complete.BuildResult); err != nil {
			return err
		}
		queue, err := privateBuildQueue(ctx, tx)
		if err != nil {
			return err
		}
		entry := queue.object(id)
		var request builddomain.ControlRequest
		if entry.text("kind") != "local-upload" || !entry.boolean("goBuild") || json.Unmarshal(entry["body"], &request) != nil {
			return builddomain.ErrState
		}
		if complete.EnvSecrets != nil {
			request.EnvSecrets = complete.EnvSecrets
		}
		active, err := activeBuilds(ctx, tx)
		if err != nil {
			return err
		}
		if r.ReplacementKey != "" {
			for _, other := range active {
				if other.ID == r.ID || !other.GoBuild || other.ReplacementKey != r.ReplacementKey {
					continue
				}
				other.SupersededBy = r.ID
				if err := requestBuildCancel(ctx, tx, &other); err != nil {
					return err
				}
			}
		}
		// Supersession may have removed other private entries. Reload before writing.
		queue, err = privateBuildQueue(ctx, tx)
		if err != nil {
			return err
		}
		if err := queue.set(id, map[string]any{"kind": "local", "body": request, "buildResult": complete.BuildResult, "goBuild": true}); err != nil {
			return err
		}
		if err := writeBuildQueue(ctx, tx, queue); err != nil {
			return err
		}
		if err := tx.QueryRowContext(ctx, `SELECT COALESCE(MAX(CAST(json_extract(payload,'$.queueOrder') AS INTEGER)),0)+1 FROM control_entities WHERE kind='buildRuns'`).Scan(&r.QueueOrder); err != nil {
			return err
		}
		r.Status = "queued"
		r.QueueManaged = true
		r.Message = "Local upload accepted; waiting for deployment"
		r.UpdatedAt = time.Now().Unix()
		return saveBuild(ctx, tx, r)
	})
	return r, err
}
func (s *Store) FailLocalBuild(ctx context.Context, token, id, message string) (builddomain.Run, bool, error) {
	var r builddomain.Run
	replayed := false
	if s.writer == nil {
		return r, false, ErrReadOnly
	}
	err := s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		if err := checkManagement(ctx, tx, token); err != nil {
			return err
		}
		var err error
		r, err = readBuild(ctx, tx, id)
		if err != nil {
			return err
		}
		if !r.GoBuild || r.Mode != "local" {
			return builddomain.ErrState
		}
		if builddomain.Terminal(r.Status) {
			replayed = true
			return nil
		}
		if r.Status != "running" || r.QueueManaged {
			return builddomain.ErrState
		}
		r.Status = "failed"
		r.Message = "Local build failed"
		if message != "" {
			r.Message = message
		}
		if len(r.Message) > 4000 {
			r.Message = r.Message[:4000]
		}
		r.UpdatedAt = time.Now().Unix()
		r.CompletedAt = r.UpdatedAt
		if err := discardBuild(ctx, tx, &r); err != nil {
			return err
		}
		return saveBuild(ctx, tx, r)
	})
	return r, replayed, err
}
