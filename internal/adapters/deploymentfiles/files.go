// Package deploymentfiles writes generated jobs and reconciles Traefik files
// within configured roots. It does not decide deployment or routing policy.
package deploymentfiles

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/LiuTianjie/luma/internal/deployment"
)

type Files struct {
	stacks, routes             *os.Root
	stackPath, routePath, base string
}

func New(config deployment.RenderConfig, base string) (*Files, error) {
	f := &Files{base: base}
	var err error
	f.stackPath, err = filepath.Abs(config.StackRoot)
	if err != nil {
		return nil, err
	}
	f.routePath, err = filepath.Abs(config.RoutesRoot)
	if err != nil {
		return nil, err
	}
	for _, path := range []string{f.stackPath, f.routePath} {
		if err := os.MkdirAll(path, 0755); err != nil {
			return nil, err
		}
	}
	f.stacks, err = os.OpenRoot(f.stackPath)
	if err != nil {
		return nil, err
	}
	f.routes, err = os.OpenRoot(f.routePath)
	if err != nil {
		f.stacks.Close()
		return nil, err
	}
	return f, nil
}
func (f *Files) Close() { f.stacks.Close(); f.routes.Close() }
func (f *Files) target(path string, route bool) (*os.Root, string, string, error) {
	root, prefix := f.stacks, f.stackPath
	if route {
		root, prefix = f.routes, f.routePath
	}
	if !filepath.IsAbs(path) {
		path = filepath.Join(f.base, path)
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return nil, "", "", err
	}
	relative, err := filepath.Rel(prefix, absolute)
	if err != nil || relative == "." || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return nil, "", "", fmt.Errorf("%w: generated file must be within its configured root", deployment.ErrInput)
	}
	return root, relative, absolute, nil
}
func (f *Files) Validate(plan deployment.Plan, shadows []string) error {
	for _, a := range plan.Artifacts {
		if a.Kind != "job" && a.Kind != "route" {
			return fmt.Errorf("unsupported artifact kind")
		}
		if _, _, _, err := f.target(a.Path, a.Kind == "route"); err != nil {
			return err
		}
	}
	for _, p := range shadows {
		if _, _, _, err := f.target(p, true); err != nil {
			return err
		}
	}
	return nil
}
func (f *Files) write(ctx context.Context, a deployment.Artifact) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	root, name, absolute, err := f.target(a.Path, a.Kind == "route")
	if err != nil {
		return "", err
	}
	if err := root.MkdirAll(filepath.Dir(name), 0755); err != nil {
		return "", err
	}
	var nonce [16]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return "", err
	}
	// The temporary name has no YAML/TOML suffix, so Traefik does not parse it.
	temp := filepath.Join(filepath.Dir(name), ".luma-"+hex.EncodeToString(nonce[:])+".tmp")
	mode := os.FileMode(0600)
	if a.Kind == "route" {
		mode = 0644
	}
	file, err := root.OpenFile(temp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode)
	if err != nil {
		return "", err
	}
	defer root.Remove(temp)
	_, err = file.WriteString(a.Content)
	if err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		return "", err
	}
	if err := root.Rename(temp, name); err != nil {
		return "", err
	}
	dir, err := root.Open(filepath.Dir(name))
	if err != nil {
		return "", err
	}
	defer dir.Close()
	if err := dir.Sync(); err != nil {
		return "", err
	}
	return absolute, nil
}
func (f *Files) WriteJobs(ctx context.Context, plan deployment.Plan) ([]string, error) {
	written := []string{}
	for _, a := range plan.Artifacts {
		if a.Kind == "job" {
			path, err := f.write(ctx, a)
			if err != nil {
				return written, err
			}
			written = append(written, path)
		}
	}
	return written, nil
}
func (f *Files) ReconcileRoutes(ctx context.Context, plan deployment.Plan, shadows []string) ([]string, error) {
	written := []string{}
	for _, a := range plan.Artifacts {
		if a.Kind == "route" {
			path, err := f.write(ctx, a)
			if err != nil {
				return written, err
			}
			written = append(written, path)
		}
	}
	for _, path := range shadows {
		if err := ctx.Err(); err != nil {
			return written, err
		}
		root, name, _, err := f.target(path, true)
		if err != nil {
			return written, err
		}
		info, err := root.Stat(name)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return written, err
		}
		if !info.Mode().IsRegular() {
			return written, fmt.Errorf("route shadow is not a regular file")
		}
		if err := root.Remove(name); err != nil {
			return written, err
		}
		dir, err := root.Open(filepath.Dir(name))
		if err != nil {
			return written, err
		}
		err = dir.Sync()
		dir.Close()
		if err != nil {
			return written, err
		}
	}
	return written, nil
}
