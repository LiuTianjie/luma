package builderfs

import (
	"archive/tar"
	"context"
	"fmt"
	"io"
	"os"
	"path"
	"strings"

	builddomain "github.com/LiuTianjie/luma/internal/build"
)

// Extract verifies the reserved content digest before touching the destination.
// Only a new, private directory can be materialized. Partial output is removed;
// hardlinks, duplicate paths and traversal through symlinks are never accepted.
func Extract(ctx context.Context, input io.ReadSeeker, destination, digest string, limits Limits) (err error) {
	if !builddomain.ValidContentDigest(digest) {
		return fmt.Errorf("invalid snapshot digest")
	}
	result, err := Inspect(ctx, input, limits)
	if err != nil {
		return err
	}
	if result.Digest != digest {
		return fmt.Errorf("source snapshot digest mismatch")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if err := os.Mkdir(destination, 0700); err != nil {
		return err
	}
	defer func() {
		if err != nil {
			_ = os.RemoveAll(destination)
		}
	}()
	root, err := os.OpenRoot(destination)
	if err != nil {
		return err
	}
	defer root.Close()
	actual, err := inspect(ctx, input, limits, func(hdr *tar.Header, name string, content io.Reader) error {
		if err := makeDirectories(root, path.Dir(name), 0755); err != nil {
			return err
		}
		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := makeDirectories(root, name, 0755); err != nil {
				return err
			}
		case tar.TypeSymlink:
			link := hdr.Linkname
			target := path.Clean(path.Join(path.Dir(name), link))
			if link == "" || strings.HasPrefix(link, "/") || strings.ContainsAny(link, "\x00\\") || target == ".." || strings.HasPrefix(target, "../") {
				return fmt.Errorf("source snapshot contains an unsafe symlink")
			}
			if err := root.Symlink(link, name); err != nil {
				return err
			}
		case tar.TypeReg, tar.TypeRegA:
			output, err := root.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
			if err != nil {
				return err
			}
			_, err = io.Copy(output, content)
			if err == nil {
				mode := os.FileMode(hdr.Mode) & 0755
				if mode == 0 {
					mode = 0600
				}
				err = output.Chmod(mode)
			}
			if err == nil {
				err = output.Sync()
			}
			closeErr := output.Close()
			if err != nil {
				return err
			}
			if closeErr != nil {
				return closeErr
			}
		default:
			return fmt.Errorf("unsupported source snapshot entry")
		}
		return nil
	})
	if err != nil {
		return err
	}
	if actual.Digest != digest {
		return fmt.Errorf("source snapshot changed during extraction")
	}
	return nil
}

func makeDirectories(root *os.Root, name string, mode os.FileMode) error {
	if name == "." {
		return nil
	}
	parts := strings.Split(name, "/")
	for i := range parts {
		p := strings.Join(parts[:i+1], "/")
		info, err := root.Lstat(p)
		if os.IsNotExist(err) {
			err = root.Mkdir(p, mode)
			if err != nil {
				return err
			}
			continue
		}
		if err != nil {
			return err
		}
		if !info.IsDir() {
			return fmt.Errorf("content path traverses a symlink or non-directory")
		}
	}
	return nil
}
