package builderfs

import (
	"archive/zip"
	"bytes"
	"context"
	"os"
	"path/filepath"
	"testing"
)

type zipEntry struct {
	name, body string
	mode       os.FileMode
}

func zipBytes(t *testing.T, entries ...zipEntry) []byte {
	t.Helper()
	var b bytes.Buffer
	w := zip.NewWriter(&b)
	for _, entry := range entries {
		header := &zip.FileHeader{Name: entry.name, Method: zip.Deflate}
		if entry.mode != 0 {
			header.SetMode(entry.mode)
		}
		f, err := w.CreateHeader(header)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := f.Write([]byte(entry.body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return b.Bytes()
}

func TestUploadedZIPRejectsTraversalLinksAndCaseFoldCollisions(t *testing.T) {
	cases := map[string][]zipEntry{
		"parent":        {{name: "../index.html", body: "bad"}},
		"absolute":      {{name: "/index.html", body: "bad"}},
		"backslash":     {{name: "foo\\index.html", body: "bad"}},
		"duplicate":     {{name: "index.html", body: "first"}, {name: "INDEX.HTML", body: "second"}},
		"unicode-fold":  {{name: "index.html", body: "first"}, {name: "Straße.txt", body: "one"}, {name: "STRASSE.txt", body: "two"}},
		"symlink":       {{name: "index.html", body: "target", mode: os.ModeSymlink | 0777}},
		"device":        {{name: "index.html", mode: os.ModeDevice | 0600}},
		"missing-index": {{name: "other.html", body: "bad"}},
	}
	for name, entries := range cases {
		t.Run(name, func(t *testing.T) {
			base := physicalTemp(t)
			source := filepath.Join(base, "source.zip")
			destination := filepath.Join(base, "site")
			if err := os.WriteFile(source, zipBytes(t, entries...), 0600); err != nil {
				t.Fatal(err)
			}
			if err := (Store{}).MaterializeObject(context.Background(), source, destination, "application/zip", testLimits); err == nil {
				t.Fatal("unsafe archive accepted")
			}
			if _, err := os.Stat(destination); !os.IsNotExist(err) {
				t.Fatal("partial archive retained")
			}
		})
	}
}

func TestUploadedZIPAccountsForArchiveAndExpandedFiles(t *testing.T) {
	base := physicalTemp(t)
	source := filepath.Join(base, "source.zip")
	raw := zipBytes(t, zipEntry{name: "index.html", body: string(bytes.Repeat([]byte("x"), 10000))})
	if err := os.WriteFile(source, raw, 0600); err != nil {
		t.Fatal(err)
	}
	limits := Limits{MaxBytes: 10000, MaxEntries: 10}
	if err := (Store{}).MaterializeObject(context.Background(), source, filepath.Join(base, "site"), "application/zip", limits); err == nil {
		t.Fatal("expanded workspace exceeded budget")
	}
	limits.MaxBytes = 20000
	if err := (Store{}).MaterializeObject(context.Background(), source, filepath.Join(base, "site"), "application/zip", limits); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(source); !os.IsNotExist(err) {
		t.Fatal("uploaded archive retained after materialization")
	}
}
