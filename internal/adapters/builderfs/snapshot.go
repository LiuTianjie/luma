// Package builderfs owns Builder source snapshots and content-addressed files.
// It does not clone repositories, execute analyzers or make network requests.
package builderfs

import (
	"archive/tar"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"hash"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	builddomain "github.com/LiuTianjie/luma/internal/build"
)

type Limits = builddomain.SourceLimits

type contextReader struct {
	ctx   context.Context
	input io.Reader
}

func (r contextReader) Read(p []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.input.Read(p)
}

type boundedWriter struct {
	output    io.Writer
	remaining int64
}

func (w *boundedWriter) Write(p []byte) (int, error) {
	if int64(len(p)) > w.remaining {
		return 0, fmt.Errorf("source snapshot exceeds the disk budget")
	}
	n, err := w.output.Write(p)
	w.remaining -= int64(n)
	return n, err
}

// Create snapshots the current tree (including uncommitted files), excluding
// only its top-level .git entry. Metadata is normalized; directory siblings are
// emitted before descending, matching the previous logical tree hash ordering.
// destination must not exist and is removed if creation or validation fails.
func Create(ctx context.Context, source, destination string, limits Limits) (result builddomain.Snapshot, err error) {
	if err = limits.Validate(); err != nil {
		return
	}
	root, err := os.OpenRoot(source)
	if err != nil {
		return result, err
	}
	defer root.Close()
	// A snapshot cannot live in the source being enumerated.
	sourceReal, err := filepath.EvalSymlinks(source)
	if err != nil {
		return result, err
	}
	parentReal, err := filepath.EvalSymlinks(filepath.Dir(destination))
	if err != nil {
		return result, err
	}
	rel, err := filepath.Rel(sourceReal, filepath.Join(parentReal, filepath.Base(destination)))
	if err != nil || (rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))) {
		return result, fmt.Errorf("snapshot destination must be outside the source tree")
	}
	output, err := os.OpenFile(destination, os.O_CREATE|os.O_EXCL|os.O_RDWR, 0600)
	if err != nil {
		return result, err
	}
	defer func() {
		_ = output.Close()
		if err != nil {
			_ = os.Remove(destination)
		}
	}()
	w := tar.NewWriter(&boundedWriter{output: output, remaining: limits.MaxBytes})
	count := 0
	var walk func(string) error
	walk = func(directory string) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		info, err := root.Lstat(directory)
		if err != nil || !info.IsDir() {
			return fmt.Errorf("source directory changed")
		}
		file, err := root.Open(directory)
		if err != nil {
			return err
		}
		opened, statErr := file.Stat()
		if statErr != nil || !os.SameFile(info, opened) {
			file.Close()
			return fmt.Errorf("source directory changed")
		}
		entries, err := file.ReadDir(limits.MaxEntries - count + 2)
		file.Close()
		if err != nil && err != io.EOF {
			return err
		}
		slices.SortFunc(entries, func(a, b fs.DirEntry) int { return strings.Compare(a.Name(), b.Name()) })
		var directories []string
		for _, entry := range entries {
			if directory == "." && entry.Name() == ".git" {
				continue
			}
			if err := ctx.Err(); err != nil {
				return err
			}
			count++
			if count > limits.MaxEntries {
				return fmt.Errorf("source tree has too many entries")
			}
			name := path.Join(directory, entry.Name())
			if !utf8.ValidString(name) {
				return fmt.Errorf("source path is not UTF-8")
			}
			info, err := root.Lstat(name)
			if err != nil {
				return err
			}
			h := &tar.Header{Name: name, ModTime: time.Unix(0, 0), Format: tar.FormatPAX}
			switch {
			case info.IsDir():
				h.Typeflag, h.Mode = tar.TypeDir, 0755
				directories = append(directories, name)
			case info.Mode()&os.ModeSymlink != 0:
				h.Typeflag, h.Mode = tar.TypeSymlink, 0777
				h.Linkname, err = root.Readlink(name)
				if err != nil {
					return err
				}
				if !utf8.ValidString(h.Linkname) {
					return fmt.Errorf("source symlink is not UTF-8")
				}
			case info.Mode().IsRegular():
				h.Typeflag, h.Mode, h.Size = tar.TypeReg, 0644, info.Size()
				if info.Mode().Perm()&0111 != 0 {
					h.Mode = 0755
				}
			default:
				return fmt.Errorf("source tree contains an unsupported file type")
			}
			if err := w.WriteHeader(h); err != nil {
				return err
			}
			if h.Typeflag != tar.TypeReg {
				continue
			}
			input, err := root.Open(name)
			if err != nil {
				return err
			}
			actual, err := input.Stat()
			if err != nil || !actual.Mode().IsRegular() || !os.SameFile(info, actual) || actual.Size() != info.Size() {
				input.Close()
				return fmt.Errorf("source file changed during snapshot")
			}
			_, err = io.CopyN(w, contextReader{ctx, input}, h.Size)
			input.Close()
			if err != nil {
				return err
			}
		}
		for _, directory := range directories {
			if err := walk(directory); err != nil {
				return err
			}
		}
		return nil
	}
	if err = walk("."); err != nil {
		return result, err
	}
	if err = w.Close(); err != nil {
		return result, err
	}
	if err = output.Sync(); err != nil {
		return result, err
	}
	if _, err = output.Seek(0, io.SeekStart); err != nil {
		return result, err
	}
	// Hash the completed archive, not a second read of a mutable checkout.
	result, err = Inspect(ctx, output, limits)
	return result, err
}

// Inspect hashes both exact archive bytes and logical contents. It accepts
// existing Python PAX archives and Go PAX archives without re-encoding either.
func Inspect(ctx context.Context, input io.ReadSeeker, limits Limits) (builddomain.Snapshot, error) {
	return inspect(ctx, input, limits, nil)
}

func inspect(ctx context.Context, input io.ReadSeeker, limits Limits, visit func(*tar.Header, string, io.Reader) error) (builddomain.Snapshot, error) {
	var result builddomain.Snapshot
	if err := limits.Validate(); err != nil {
		return result, err
	}
	if _, err := input.Seek(0, io.SeekStart); err != nil {
		return result, err
	}
	h := sha256.New()
	limited := &io.LimitedReader{R: contextReader{ctx, input}, N: limits.MaxBytes + 1}
	raw := io.TeeReader(limited, h)
	archive := tar.NewReader(raw)
	tree := sha256.New()
	seen := map[string]bool{}
	for {
		if err := ctx.Err(); err != nil {
			return result, err
		}
		hdr, err := archive.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return result, err
		}
		name, err := safePath(hdr.Name)
		if err != nil {
			return result, err
		}
		if seen[name] {
			return result, fmt.Errorf("duplicate source snapshot path")
		}
		seen[name] = true
		if len(seen) > limits.MaxEntries {
			return result, fmt.Errorf("source snapshot has too many entries")
		}
		kind, payload := "", ""
		size := int64(0)
		var fileHash hash.Hash
		var content io.Reader = archive
		switch hdr.Typeflag {
		case tar.TypeDir:
			kind = "directory"
		case tar.TypeSymlink:
			if !utf8.ValidString(hdr.Linkname) {
				return result, fmt.Errorf("source symlink is not UTF-8")
			}
			kind = "symlink"
			sum := sha256.Sum256([]byte(hdr.Linkname))
			payload = hex.EncodeToString(sum[:])
		case tar.TypeReg, tar.TypeRegA:
			kind, size = "file", hdr.Size
			if size > limits.MaxBytes-result.SourceBytes {
				return result, fmt.Errorf("source snapshot expands beyond the disk budget")
			}
			result.SourceBytes += size
			fileHash = sha256.New()
			content = io.TeeReader(archive, fileHash)
		default:
			return result, fmt.Errorf("unsupported source snapshot entry")
		}
		if visit != nil {
			if err := visit(hdr, name, content); err != nil {
				return result, err
			}
		}
		if _, err := io.Copy(io.Discard, content); err != nil {
			return result, err
		}
		if fileHash != nil {
			payload = hex.EncodeToString(fileHash.Sum(nil))
		}
		record := []byte(`{"digest":` + quoteJSON(payload) + `,"kind":` + quoteJSON(kind) + `,"mode":` + strconv.FormatInt(hdr.Mode, 10) + `,"path":` + quoteJSON(name) + `,"size":` + strconv.FormatInt(size, 10) + `}`)
		var prefix [8]byte
		binary.BigEndian.PutUint64(prefix[:], uint64(len(record)))
		tree.Write(prefix[:])
		tree.Write(record)
	}
	// Include tar padding in the content digest, in the same read that produced
	// the tree hash (and, during extraction, the actual materialized bytes).
	if _, err := io.Copy(io.Discard, raw); err != nil {
		return result, err
	}
	n := limits.MaxBytes + 1 - limited.N
	if n == 0 || n > limits.MaxBytes {
		return result, fmt.Errorf("source snapshot exceeds the disk budget or is empty")
	}
	result.Digest, result.ArchiveBytes = digestString(h), n
	result.TreeDigest = digestString(tree)
	return result, nil
}

func digestString(h hash.Hash) string { return "sha256:" + hex.EncodeToString(h.Sum(nil)) }

func safePath(value string) (string, error) {
	value = strings.TrimSuffix(value, "/") // tar directory names conventionally end with '/'.
	if !fs.ValidPath(value) || value == "." || !utf8.ValidString(value) || strings.ContainsAny(value, "\x00\\") {
		return "", fmt.Errorf("source snapshot contains an unsafe path")
	}
	return value, nil
}

// Python's canonical tree records keep UTF-8 (including U+2028/U+2029 and HTML
// characters) literal. encoding/json always escapes the two separators.
func quoteJSON(value string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range value {
		switch r {
		case '"', '\\':
			b.WriteByte('\\')
			b.WriteRune(r)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		default:
			if r < 0x20 {
				fmt.Fprintf(&b, `\u%04x`, r)
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
	return b.String()
}
