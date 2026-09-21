package builderfs

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"fmt"
	"io"
	"os"
	"path"
	"strings"
	"sync"

	builddomain "github.com/LiuTianjie/luma/internal/build"
)

const DefaultRoot = "/var/lib/luma/builder/snapshots"

// Store uses the existing Builder layout; migration never relocates its data.
// Root comes from Agent configuration, never from a task payload.
type Store struct{ Root string }

// Content retains the execution lock until Close so cleanup cannot remove a
// snapshot while it is being built or exported. Callers must always close it.
type Content struct {
	*os.File
	once     sync.Once
	release  func()
	closeErr error
}

func (c *Content) Close() error {
	c.once.Do(func() { c.closeErr = c.File.Close(); c.release() })
	return c.closeErr
}

func contentPath(namespace, digest, suffix string) (string, error) {
	if !builddomain.ValidContentDigest(digest) {
		return "", fmt.Errorf("invalid content digest")
	}
	hex := strings.TrimPrefix(digest, "sha256:")
	return path.Join(namespace, hex[:2], hex+suffix), nil
}

func (s Store) PutSnapshot(ctx context.Context, source io.ReadSeeker, digest string, limits Limits) error {
	result, err := Inspect(ctx, source, limits)
	if err != nil {
		return err
	}
	if result.Digest != digest {
		return fmt.Errorf("snapshot digest mismatch")
	}
	name, err := contentPath("sha256", digest, ".tar")
	if err != nil {
		return err
	}
	return s.put(ctx, source, name, digest, result.ArchiveBytes)
}

func (s Store) OpenSnapshot(ctx context.Context, digest string, limits Limits) (*Content, error) {
	if err := limits.Validate(); err != nil {
		return nil, err
	}
	name, err := contentPath("sha256", digest, ".tar")
	if err != nil {
		return nil, err
	}
	return s.open(ctx, name, digest, 0, limits.MaxBytes)
}

func (s Store) Capture(ctx context.Context, source, destination string, limits Limits) (builddomain.Snapshot, error) {
	return Create(ctx, source, destination, limits)
}

func (s Store) Materialize(ctx context.Context, input io.ReadSeeker, destination, digest string, limits Limits) error {
	return Extract(ctx, input, destination, digest, limits)
}

func (s Store) PutArtifact(ctx context.Context, source io.ReadSeeker, descriptor builddomain.AnalysisArtifact) error {
	namespace, err := descriptor.StoreNamespace()
	if err != nil {
		return err
	}
	name, _ := contentPath(namespace, descriptor.Digest, ".json")
	return s.put(ctx, source, name, descriptor.Digest, descriptor.SizeBytes)
}

func (s Store) OpenArtifact(ctx context.Context, request builddomain.ArtifactExportRequest) (*Content, error) {
	if err := request.Validate(); err != nil {
		return nil, err
	}
	namespace, _ := request.Artifact.StoreNamespace()
	name, _ := contentPath(namespace, request.Artifact.Digest, ".json")
	return s.open(ctx, name, request.Artifact.Digest, request.Artifact.SizeBytes, builddomain.MaxAnalysisArtifactBytes)
}

func (s Store) open(ctx context.Context, name, digest string, exact, maximum int64) (*Content, error) {
	root, release, err := s.acquire()
	if err != nil {
		return nil, err
	}
	defer root.Close()
	f, err := openRegular(root, name)
	if err != nil {
		release()
		return nil, err
	}
	if err := verify(ctx, f, digest, exact, maximum); err != nil {
		f.Close()
		release()
		return nil, err
	}
	return &Content{File: f, release: release}, nil
}

func (s Store) put(ctx context.Context, source io.ReadSeeker, name, digest string, size int64) error {
	if err := verify(ctx, source, digest, size, size); err != nil {
		return err
	}
	root, release, err := s.acquire()
	if err != nil {
		return err
	}
	defer release()
	defer root.Close()
	if err := makeDirectories(root, path.Dir(name), 0700); err != nil {
		return err
	}
	if err := privateParents(root, path.Dir(name)); err != nil {
		return err
	}
	if existing, err := openRegular(root, name); err == nil {
		defer existing.Close()
		return verify(ctx, existing, digest, size, size)
	} else if !os.IsNotExist(err) {
		return err
	}
	tmp := path.Join(path.Dir(name), fmt.Sprintf(".content-%x.tmp", randomBytes()))
	output, err := root.OpenFile(tmp, os.O_CREATE|os.O_EXCL|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer output.Close()
	defer root.Remove(tmp)
	_, err = io.Copy(output, io.LimitReader(contextReader{ctx, source}, size+1))
	if err != nil {
		return err
	}
	// Verify the copied bytes, not just the source before copying.
	if err := verify(ctx, output, digest, size, size); err != nil {
		return err
	}
	if err := output.Sync(); err != nil {
		return err
	}
	// Link publishes without overwriting a digest path if another writer exists.
	if err := root.Link(tmp, name); err != nil {
		return err
	}
	if err := root.Remove(tmp); err != nil {
		return err
	}
	dir, err := root.Open(path.Dir(name))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func randomBytes() []byte { value := make([]byte, 16); _, _ = rand.Read(value); return value }

func verify(ctx context.Context, input io.ReadSeeker, digest string, exact, maximum int64) error {
	if _, err := input.Seek(0, io.SeekStart); err != nil {
		return err
	}
	h := sha256.New()
	n, err := io.Copy(h, io.LimitReader(contextReader{ctx, input}, maximum+1))
	if err != nil {
		return err
	}
	if n < 1 || n > maximum || (exact > 0 && n != exact) || digestString(h) != digest {
		return fmt.Errorf("content size or digest mismatch")
	}
	_, err = input.Seek(0, io.SeekStart)
	return err
}

func openRegular(root *os.Root, name string) (*os.File, error) {
	if err := privateParents(root, path.Dir(name)); err != nil {
		return nil, err
	}
	// Lstat every parent, including in-store symlinks that Root would otherwise
	// allow. The configured store is owned by this Agent.
	parts := strings.Split(name, "/")
	for i := range parts {
		info, err := root.Lstat(strings.Join(parts[:i+1], "/"))
		if err != nil {
			return nil, err
		}
		if i < len(parts)-1 {
			if !info.IsDir() {
				return nil, fmt.Errorf("unsafe content directory")
			}
		} else if !info.Mode().IsRegular() {
			return nil, fmt.Errorf("content must be a regular file")
		}
	}
	f, err := openNoFollow(root, name)
	if err != nil {
		return nil, err
	}
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() {
		f.Close()
		return nil, fmt.Errorf("content must be a regular file")
	}
	return f, nil
}
