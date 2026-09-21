package goarchitecture

import (
	"go/parser"
	"go/token"
	"io/fs"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func TestImportBoundaries(t *testing.T) {
	root := filepath.Join("..", "..", "internal")
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		rel, _ := filepath.Rel(root, path)
		parts := strings.Split(filepath.ToSlash(rel), "/")
		file, err := parser.ParseFile(token.NewFileSet(), path, nil, parser.ImportsOnly)
		if err != nil {
			return err
		}
		for _, spec := range file.Imports {
			imp, _ := strconv.Unquote(spec.Path.Value)
			if parts[0] == "deployment" || parts[0] == "region" || parts[0] == "storage" || parts[0] == "node" || parts[0] == "registry" || parts[0] == "observability" || parts[0] == "credentials" || parts[0] == "history" || parts[0] == "build" || parts[0] == "workflow" {
				// net/url parses URL syntax without doing network I/O.
				if strings.Contains(imp, "/adapters/") || strings.HasSuffix(imp, "/cli") || strings.HasSuffix(imp, "/agentapp") || strings.HasSuffix(imp, "/controlapp") || (strings.HasPrefix(imp, "net/") && imp != "net/url") || imp == "os" || imp == "os/exec" || imp == "database/sql" || strings.Contains(imp, "yaml") || strings.Contains(imp, "sqlite") {
					t.Errorf("business package %s imports I/O or adapter %s", rel, imp)
				}
			}
			if parts[0] == "region" && strings.Contains(imp, "/internal/") {
				t.Errorf("region policy must not depend on another Luma module: %s", imp)
			}
			if parts[0] == "adapters" {
				if strings.HasSuffix(imp, "/cli") || strings.HasSuffix(imp, "/agentapp") || strings.HasSuffix(imp, "/controlapp") {
					t.Errorf("adapter %s imports CLI", rel)
				}
				if strings.Contains(imp, "/adapters/") && !strings.HasPrefix(imp, "github.com/LiuTianjie/luma/internal/adapters/"+parts[1]) {
					t.Errorf("adapter %s imports another adapter %s", rel, imp)
				}
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}
