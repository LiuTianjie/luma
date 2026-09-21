package builderfs

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

var testLimits = Limits{MaxBytes: 8 << 20, MaxEntries: 1000}

func physicalTemp(t *testing.T) string {
	t.Helper()
	p, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return p
}
func writeFile(t *testing.T, name, body string, mode os.FileMode) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(name), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(name, []byte(body), mode); err != nil {
		t.Fatal(err)
	}
}
func createFile(t *testing.T, source, destination string) []byte {
	t.Helper()
	if _, err := Create(context.Background(), source, destination, testLimits); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(destination)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
func hashBytes(raw []byte) string { return fmt.Sprintf("sha256:%x", sha256.Sum256(raw)) }

func TestSnapshotDeterminismDirtyFilesAndRoundTrip(t *testing.T) {
	ctx := context.Background()
	base := physicalTemp(t)
	source := filepath.Join(base, "source")
	writeFile(t, filepath.Join(source, ".git", "ignored"), "private git", 0600)
	writeFile(t, filepath.Join(source, "a", "inner", "test.sh"), "#!/bin/sh\necho working-tree\n", 0755)
	writeFile(t, filepath.Join(source, "a", ".git", "keep"), "nested .git retained", 0600)
	writeFile(t, filepath.Join(source, "中文\u2028<&>.txt"), "uncommitted contents", 0600)
	if err := os.Symlink("a/inner/test.sh", filepath.Join(source, "run")); err != nil {
		t.Fatal(err)
	}
	first := createFile(t, source, filepath.Join(base, "one.tar"))
	if err := os.Chtimes(filepath.Join(source, "a", "inner", "test.sh"), time.Now(), time.Now()); err != nil {
		t.Fatal(err)
	}
	second := createFile(t, source, filepath.Join(base, "two.tar"))
	if !bytes.Equal(first, second) {
		t.Fatal("metadata changed deterministic archive")
	}
	result, err := Inspect(ctx, bytes.NewReader(first), testLimits)
	if err != nil {
		t.Fatal(err)
	}
	if result.Digest != hashBytes(first) {
		t.Fatal("wrong archive digest")
	}
	dest := filepath.Join(base, "materialized")
	if err := Extract(ctx, bytes.NewReader(first), dest, result.Digest, testLimits); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dest, ".git")); !os.IsNotExist(err) {
		t.Fatal("top-level git metadata was retained")
	}
	if raw, err := os.ReadFile(filepath.Join(dest, "run")); err != nil || string(raw) != "#!/bin/sh\necho working-tree\n" {
		t.Fatalf("symlink content: %q, %v", raw, err)
	}
	if mode, _ := os.Stat(filepath.Join(dest, "a", "inner", "test.sh")); mode.Mode().Perm() != 0755 {
		t.Fatal("executable bit lost")
	}
	recreated := createFile(t, dest, filepath.Join(base, "three.tar"))
	if !bytes.Equal(first, recreated) {
		t.Fatal("materialization changed logical tree")
	}
	writeFile(t, filepath.Join(source, "中文\u2028<&>.txt"), "new uncommitted contents", 0600)
	third := createFile(t, source, filepath.Join(base, "dirty.tar"))
	if bytes.Equal(third, first) {
		t.Fatal("dirty working tree was not read")
	}
}

type tarEntry struct {
	name, link, body string
	kind             byte
	mode             int64
}

func archiveBytes(t *testing.T, entries ...tarEntry) []byte {
	t.Helper()
	var b bytes.Buffer
	w := tar.NewWriter(&b)
	for _, e := range entries {
		h := &tar.Header{Name: e.name, Linkname: e.link, Mode: e.mode, Typeflag: e.kind, Format: tar.FormatPAX}
		if e.kind == tar.TypeReg {
			h.Size = int64(len(e.body))
		}
		if err := w.WriteHeader(h); err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write([]byte(e.body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return b.Bytes()
}

func TestExtractRejectsUnsafeArchivesAndPreservesExistingDestination(t *testing.T) {
	ctx := context.Background()
	base := physicalTemp(t)
	cases := map[string][]tarEntry{
		"parent":             {{name: "../outside", kind: tar.TypeReg, body: "bad"}},
		"absolute":           {{name: "/outside", kind: tar.TypeReg, body: "bad"}},
		"duplicate":          {{name: "same", kind: tar.TypeReg}, {name: "same", kind: tar.TypeReg}},
		"hardlink":           {{name: "hard", kind: tar.TypeLink, link: "outside"}},
		"device":             {{name: "device", kind: tar.TypeChar}},
		"escaping-link":      {{name: "link", kind: tar.TypeSymlink, link: "../outside"}},
		"absolute-link":      {{name: "link", kind: tar.TypeSymlink, link: "/outside"}},
		"write-through-link": {{name: "real", kind: tar.TypeDir}, {name: "link", kind: tar.TypeSymlink, link: "real"}, {name: "link/evil", kind: tar.TypeReg, body: "bad"}},
		"dir-through-link":   {{name: "link", kind: tar.TypeSymlink, link: "real"}, {name: "link/nested", kind: tar.TypeDir}},
	}
	for label, entries := range cases {
		t.Run(label, func(t *testing.T) {
			raw := archiveBytes(t, entries...)
			dest := filepath.Join(base, label)
			if err := Extract(ctx, bytes.NewReader(raw), dest, hashBytes(raw), testLimits); err == nil {
				t.Fatal("unsafe archive accepted")
			}
			if _, err := os.Stat(dest); !os.IsNotExist(err) {
				t.Fatal("partial extraction retained")
			}
		})
	}
	raw := archiveBytes(t, tarEntry{name: "safe", kind: tar.TypeReg, body: "contents"})
	existing := filepath.Join(base, "existing")
	writeFile(t, filepath.Join(existing, "preserved"), "keep", 0600)
	if err := Extract(ctx, bytes.NewReader(raw), existing, hashBytes(raw), testLimits); err == nil {
		t.Fatal("existing destination accepted")
	}
	if data, _ := os.ReadFile(filepath.Join(existing, "preserved")); string(data) != "keep" {
		t.Fatal("existing data changed")
	}
	if err := Extract(ctx, bytes.NewReader(raw), filepath.Join(base, "wrong"), "sha256:"+strings.Repeat("0", 64), testLimits); err == nil {
		t.Fatal("wrong digest accepted")
	}
}

func TestSnapshotBudgetsCancellationAndDestination(t *testing.T) {
	base := physicalTemp(t)
	source := filepath.Join(base, "source")
	writeFile(t, filepath.Join(source, "file"), strings.Repeat("x", 4096), 0600)
	for name, limits := range map[string]Limits{"disk": {MaxBytes: 512, MaxEntries: 5}, "count": {MaxBytes: 1 << 20, MaxEntries: 1}} {
		writeFile(t, filepath.Join(source, "second"), "y", 0600)
		dest := filepath.Join(base, name)
		if _, err := Create(context.Background(), source, dest, limits); err == nil {
			t.Fatalf("%s limit ignored", name)
		}
		if _, err := os.Stat(dest); !os.IsNotExist(err) {
			t.Fatal("failed snapshot retained")
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := Create(ctx, source, filepath.Join(base, "canceled"), testLimits); err == nil {
		t.Fatal("cancellation ignored")
	}
	if _, err := Create(context.Background(), source, filepath.Join(source, "recursive.tar"), testLimits); err == nil {
		t.Fatal("snapshot inside source allowed")
	}
	if _, err := os.Stat(filepath.Join(source, "recursive.tar")); !os.IsNotExist(err) {
		t.Fatal("created recursive archive")
	}
}

type changingArchive struct {
	*bytes.Reader
	next  []byte
	seeks int
}

func (r *changingArchive) Seek(offset int64, whence int) (int64, error) {
	if offset == 0 && whence == io.SeekStart {
		r.seeks++
		if r.seeks == 2 {
			r.Reader = bytes.NewReader(r.next)
		}
	}
	return r.Reader.Seek(offset, whence)
}

func TestExtractBindsWrittenBytesToVerifiedDigest(t *testing.T) {
	before := archiveBytes(t, tarEntry{name: "app", kind: tar.TypeReg, body: "original"})
	after := archiveBytes(t, tarEntry{name: "app", kind: tar.TypeReg, body: "modified"})
	input := &changingArchive{Reader: bytes.NewReader(before), next: after}
	dest := filepath.Join(physicalTemp(t), "changed")
	if err := Extract(context.Background(), input, dest, hashBytes(before), testLimits); err == nil {
		t.Fatal("changed bytes accepted after verification")
	}
	if _, err := os.Stat(dest); !os.IsNotExist(err) {
		t.Fatal("unverified output retained")
	}
}
