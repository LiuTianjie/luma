// Package registryhttp resolves mutable image references through the Registry V2
// protocol. It reads manifests only; image transfers run on the selected Agent.
package registryhttp

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/LiuTianjie/luma/internal/registry"
	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/go-containerregistry/pkg/v1/remote/transport"
)

type Resolver struct{ Transport http.RoundTripper }

func (r Resolver) Resolve(ctx context.Context, image string, auth registry.RuntimeAuth) (string, error) {
	parsed, err := registry.ParseImage(image)
	if err != nil {
		return "", err
	}
	if parsed.Digest != "" {
		return parsed.Reference, nil
	}
	ref, err := name.ParseReference(image)
	if err != nil {
		return "", fmt.Errorf("invalid registry image reference")
	}
	if auth.ServerAddress != "" {
		authImage, err := registry.ParseImage(auth.ServerAddress + "/" + parsed.Path)
		if err != nil || !registry.SameRepository(parsed, authImage) {
			return "", fmt.Errorf("registry credential does not match the requested image")
		}
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	options := []remote.Option{remote.WithContext(ctx), remote.WithAuth(authn.FromConfig(authn.AuthConfig{Username: auth.Username, Password: auth.Password}))}
	if r.Transport != nil {
		options = append(options, remote.WithTransport(r.Transport))
	}
	descriptor, err := remote.Head(ref, options...)
	if err != nil {
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		var status *transport.Error
		if errors.As(err, &status) {
			return "", fmt.Errorf("registry manifest lookup failed for %s: HTTP %d", image, status.StatusCode)
		}
		// Error bodies and token endpoint URLs can contain authentication material.
		return "", fmt.Errorf("registry manifest lookup failed for %s", image)
	}
	digest := descriptor.Digest.String()
	if !registry.ValidDigest(digest) {
		return "", fmt.Errorf("registry manifest did not return a sha256 digest")
	}
	return parsed.Repository + "@" + digest, nil
}
