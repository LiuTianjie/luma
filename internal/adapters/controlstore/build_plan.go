package controlstore

import (
	"context"
	"database/sql"
	"fmt"
	"net/url"
	"path"
	"regexp"
	"slices"
	"strings"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/deployment"
)

var repositoryPart = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
var imageRepositoryInvalid = regexp.MustCompile(`[^a-z0-9._/-]+`)

func (s *Store) PlanBuild(ctx context.Context, r builddomain.ControlRequest, config deployment.RenderConfig) (builddomain.Planned, error) {
	var plan builddomain.Planned
	if r.ProxyMode != "" && r.ProxyMode != "auto" && r.ProxyMode != "direct" {
		return plan, fmt.Errorf("%w: proxyMode must be auto or direct", builddomain.ErrInput)
	}
	if r.ProxyMode == "direct" {
		if r.Proxy != nil && *r.Proxy != "" {
			return plan, fmt.Errorf("%w: direct mode cannot specify a proxy", builddomain.ErrInput)
		}
		direct := ""
		r.Proxy = &direct
	}
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		snapshot, _, err := deploymentState(ctx, tx, "service", "")
		if err != nil {
			return err
		}
		if r.RepoURL == "" {
			if r.ProviderID == "" || !repositoryPart.MatchString(r.Repository) {
				return fmt.Errorf("%w: repoUrl or providerId and owner/repository are required", builddomain.ErrInput)
			}
			providers, err := configDocument(ctx, tx, "gitProviders", 8<<20)
			if err != nil {
				return err
			}
			base, err := providerCloneBase(providers.object(r.ProviderID))
			if err != nil {
				return err
			}
			r.RepoURL = base + "/" + strings.TrimSuffix(r.Repository, ".git") + ".git"
		}
		if repositoryPart.MatchString(r.RepoURL) {
			r.RepoURL = "https://github.com/" + r.RepoURL
		}
		r.RepoURL, err = builddomain.NormalizeRepository(r.RepoURL)
		if err != nil {
			return fmt.Errorf("%w: %s", builddomain.ErrInput, err)
		}
		if r.BuildNode == "" {
			r.BuildNode = snapshot.Cache.Builder
		}
		if r.BuildNode == "" {
			r.BuildNode = "builder"
		}
		builder, ok := snapshot.ImageNodes[r.BuildNode]
		if !ok {
			for _, n := range snapshot.ImageNodes {
				if slices.Contains(n.Aliases, r.BuildNode) {
					if ok {
						return fmt.Errorf("ambiguous build node")
					}
					builder = n
					ok = true
				}
			}
		}
		if !ok || !builder.Builder || !builder.Ready || !slices.Contains(builder.Capabilities, "docker-build") || builder.SupportedActions != nil && !slices.Contains(*builder.SupportedActions, "build-image") {
			return fmt.Errorf("%w: build node must be declared and have a ready build-image Agent", builddomain.ErrInput)
		}
		r.BuildNode = builder.Name
		if r.RegistryHost == "" {
			r.RegistryHost = snapshot.Cache.PullHost
		}
		if r.RegistryHost == "" {
			r.RegistryHost = snapshot.NodeAddresses[builder.Name].Address + ":5000"
		}
		if r.PushHost == "" {
			r.PushHost = snapshot.Cache.PushHost
		}
		if r.PushHost == "" {
			r.PushHost = "localhost:5000"
		}
		imageRepo := repositoryProject(r.RepoURL)
		proxy := ""
		if r.Proxy != nil {
			proxy = *r.Proxy
		} else if !builder.DirectEgress {
			proxy, err = deployment.ResolveEgressProxy(builder.Region, snapshot.Egress, config)
			if err != nil {
				return err
			}
		}
		if r.ComposeSidecar != "" {
			clean := path.Clean(r.ComposeSidecar)
			if strings.HasPrefix(clean, "/") || clean == ".." || strings.HasPrefix(clean, "../") || strings.ContainsAny(clean, "\\\x00\r\n") {
				return builddomain.ErrInput
			}
			r.ComposeSidecar = clean
			if r.Manifest != "" {
				return fmt.Errorf("%w: composeSidecar cannot be combined with manifest", builddomain.ErrInput)
			}
		}
		image := builddomain.ImageRequest{RepoURL: r.RepoURL, Ref: r.Ref, RegistryHost: r.RegistryHost, PushHost: r.PushHost, Repo: imageRepo, Proxy: proxy, GitProviderID: r.ProviderID, BuildTimeout: r.BuildTimeout, Context: r.Context, Dockerfile: r.Dockerfile, Platform: r.Platform, Region: r.Region, ComposeSidecar: r.ComposeSidecar, TargetPlatformsByNode: map[string]string{}, TargetPlatformsByRegion: map[string][]string{}}
		for _, n := range snapshot.ImageNodes {
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
		for _, platforms := range image.TargetPlatformsByRegion {
			slices.Sort(platforms)
		}
		image, err = image.Validate()
		if err != nil {
			return fmt.Errorf("%w: %s", builddomain.ErrInput, err)
		}
		r.BuildTimeout = image.BuildTimeout
		plan = builddomain.Planned{Request: r, Image: image, ProjectKey: imageRepo}
		return nil
	})
	return plan, err
}

func repositoryProject(source string) string {
	repoPath := source
	fallbackOwner := "luma"
	if u, e := url.Parse(repoPath); e == nil && u.Host != "" {
		repoPath = u.Path
		fallbackOwner = u.Hostname()
		if u.Port() != "" {
			fallbackOwner = u.Port()
		}
	} else if strings.HasPrefix(repoPath, "git@") {
		_, repoPath, _ = strings.Cut(repoPath, ":")
	}
	parts := strings.Split(strings.Trim(repoPath, "/"), "/")
	if len(parts) == 1 {
		parts = append([]string{fallbackOwner}, parts...)
	}
	return imageRepositoryInvalid.ReplaceAllString(strings.ToLower(parts[len(parts)-2]+"/"+strings.TrimSuffix(parts[len(parts)-1], ".git")), "-")
}
