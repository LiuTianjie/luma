package builderfs

import (
	"archive/zip"
	"context"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"strings"

	"golang.org/x/text/cases"
)

// MaterializeObject consumes a verified uploaded HTML/ZIP file. ZIP sources are
// static sites with index.html, not general-purpose filesystem archives.
func (s Store) MaterializeObject(ctx context.Context, source, destination, mediaType string, limits Limits) (err error) {
	if err := limits.Validate(); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if mediaType != "text/html" && mediaType != "application/zip" {
		return fmt.Errorf("unsupported uploaded source media type")
	}
	info, err := os.Lstat(source)
	if err != nil || !info.Mode().IsRegular() || info.Size() < 1 || info.Size() > limits.MaxBytes {
		return fmt.Errorf("invalid uploaded source file")
	}
	if err := os.Mkdir(destination, 0700); err != nil {
		return err
	}
	defer func() {
		if err != nil {
			_ = os.RemoveAll(destination)
		}
	}()
	if mediaType == "text/html" {
		if err := os.Rename(source, filepath.Join(destination, "index.html")); err != nil {
			return err
		}
		return os.Chmod(filepath.Join(destination, "index.html"), 0644)
	}
	archive, err := zip.OpenReader(source)
	if err != nil {
		return fmt.Errorf("invalid uploaded source archive")
	}
	defer archive.Close()
	if len(archive.File) > limits.MaxEntries {
		return fmt.Errorf("uploaded source archive has too many entries")
	}
	root, err := os.OpenRoot(destination)
	if err != nil {
		return err
	}
	defer root.Close()
	seen := map[string]bool{}
	fold := cases.Fold()
	remaining := limits.MaxBytes - info.Size()
	for _, entry := range archive.File {
		if err := ctx.Err(); err != nil {
			return err
		}
		name, err := safePath(entry.Name)
		if err != nil {
			return err
		}
		if entry.Flags&1 != 0 {
			return fmt.Errorf("encrypted source archives are unsupported")
		}
		key := fold.String(name)
		if seen[key] {
			return fmt.Errorf("uploaded source archive contains duplicate paths")
		}
		seen[key] = true
		// External Unix modes must agree with the directory suffix; symlinks,
		// devices and other special files never become host filesystem entries.
		kind := (entry.ExternalAttrs >> 16) & 0170000
		directory := strings.HasSuffix(entry.Name, "/")
		if kind != 0 && ((directory && kind != 0040000) || (!directory && kind != 0100000)) {
			return fmt.Errorf("uploaded source archive contains a special file")
		}
		if err := makeDirectories(root, path.Dir(name), 0755); err != nil {
			return err
		}
		if directory {
			if err := makeDirectories(root, name, 0755); err != nil {
				return err
			}
			continue
		}
		if entry.UncompressedSize64 > uint64(remaining) {
			return fmt.Errorf("uploaded source archive exceeds the disk budget")
		}
		remaining -= int64(entry.UncompressedSize64)
		input, err := entry.Open()
		if err != nil {
			return fmt.Errorf("invalid uploaded source entry")
		}
		output, err := root.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0644)
		if err != nil {
			input.Close()
			return err
		}
		n, copyErr := io.Copy(output, io.LimitReader(contextReader{ctx, input}, int64(entry.UncompressedSize64)+1))
		input.Close()
		if copyErr == nil {
			copyErr = output.Sync()
		}
		closeErr := output.Close()
		if copyErr != nil {
			return copyErr
		}
		if closeErr != nil {
			return closeErr
		}
		if n != int64(entry.UncompressedSize64) {
			return fmt.Errorf("uploaded source archive size changed")
		}
	}
	index, err := root.Lstat("index.html")
	if err != nil || !index.Mode().IsRegular() {
		return fmt.Errorf("uploaded source archive is missing index.html")
	}
	return os.Remove(source)
}
