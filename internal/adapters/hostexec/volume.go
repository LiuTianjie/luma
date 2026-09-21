package hostexec

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

type volumePath struct {
	Root, Relative   string
	PreserveExisting bool
}

func volumeOperation(ctx context.Context, p volumePath, remove bool) (string, error) {
	if !filepath.IsAbs(p.Root) || strings.ContainsRune(p.Root, 0) || slices.Contains(strings.Split(p.Root, "/"), "..") {
		return "", fmt.Errorf("volume root must be absolute without ..")
	}
	if p.Relative == "" || filepath.IsAbs(p.Relative) || strings.ContainsRune(p.Relative, 0) || slices.Contains(strings.Split(p.Relative, "/"), "..") {
		return "", fmt.Errorf("volume path must be relative without ..")
	}
	rel := filepath.Clean(p.Relative)
	if rel == "." {
		return "", fmt.Errorf("refusing to modify the storage root")
	}
	rootPath := filepath.Clean(p.Root)
	if !remove {
		if err := os.MkdirAll(rootPath, 0755); err != nil {
			return "", err
		}
	}
	info, err := os.Lstat(rootPath)
	if remove && errors.Is(err, os.ErrNotExist) {
		return filepath.Join(rootPath, rel), nil
	}
	if err != nil {
		return "", err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return "", fmt.Errorf("volume root must be a real directory")
	}
	root, err := os.OpenRoot(rootPath)
	if err != nil {
		return "", err
	}
	defer root.Close()
	if err := ctx.Err(); err != nil {
		return "", err
	}
	info, err = root.Lstat(rel)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	if err == nil && info.Mode()&os.ModeSymlink != 0 {
		return "", fmt.Errorf("volume path must not be a symlink")
	}
	if remove {
		if err := root.RemoveAll(rel); err != nil {
			return "", err
		}
	} else {
		if err == nil && !info.IsDir() {
			return "", fmt.Errorf("volume path is not a directory")
		}
		if err != nil || !p.PreserveExisting {
			if err := root.MkdirAll(rel, 0777); err != nil {
				return "", err
			}
			// Chmod the opened directory, not a pathname vulnerable to replacement.
			dir, err := root.Open(rel)
			if err != nil {
				return "", err
			}
			defer dir.Close()
			if err := dir.Chmod(0777); err != nil {
				return "", err
			}
		}
	}
	return filepath.Join(rootPath, rel), nil
}
