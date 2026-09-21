package buildexec

import (
	"context"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/node"
	"go.yaml.in/yaml/v3"
)

func repoPath(root, base, value string) (string, error) {
	if value == "" || filepath.IsAbs(value) || strings.ContainsRune(value, 0) {
		return "", fmt.Errorf("invalid repository path")
	}
	canonical, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", err
	}
	root = canonical
	p, err := filepath.EvalSymlinks(filepath.Join(base, value))
	if err != nil {
		return "", fmt.Errorf("repository path does not exist: %s", value)
	}
	relative, err := filepath.Rel(root, p)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("path escapes repository")
	}
	return p, nil
}
func readYAML(path string) (map[string]any, string, error) {
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() > 4<<20 {
		return nil, "", fmt.Errorf("invalid or oversized repository YAML")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, "", err
	}
	var value map[string]any
	if yaml.Unmarshal(raw, &value) != nil || value == nil {
		return nil, "", fmt.Errorf("repository YAML must be a mapping")
	}
	return value, string(raw), nil
}
func object(m map[string]any, key string) map[string]any { v, _ := m[key].(map[string]any); return v }
func text(m map[string]any, key string) string           { v, _ := m[key].(string); return strings.TrimSpace(v) }
func first(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}
func discover(root, selected string) (string, string, error) {
	if selected != "" {
		p, err := repoPath(root, root, selected)
		if err != nil {
			return "", "", err
		}
		if ext := filepath.Ext(p); ext != ".yml" && ext != ".yaml" {
			return "", "", fmt.Errorf("selected Compose sidecar must be YAML")
		}
		return "compose", p, nil
	}
	type match struct {
		kind, path string
		depth      int
	}
	var found []match
	servicePatterns := []string{".luma.yml", ".luma.yaml", "luma.yml", "luma.yaml", "*.luma.yml", "*.luma.yaml"}
	composePatterns := []string{"luma.compose.yml", "luma.compose.yaml", ".luma.compose.yml", ".luma.compose.yaml", "*.luma.compose.yml", "*.luma.compose.yaml", "*.compose.luma.yml", "*.compose.luma.yaml", "docker-compose.luma.yml", "docker-compose.luma.yaml"}
	count := 0
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if d.Name() == ".git" {
				return filepath.SkipDir
			}
			return nil
		}
		count++
		if count > 100000 {
			return fmt.Errorf("repository has too many files")
		}
		kind := ""
		for _, pattern := range servicePatterns {
			if ok, _ := filepath.Match(pattern, d.Name()); ok {
				kind = "service"
			}
		}
		for _, pattern := range composePatterns {
			if ok, _ := filepath.Match(pattern, d.Name()); ok {
				kind = "compose"
			}
		}
		if kind == "" {
			return nil
		}
		rel, _ := filepath.Rel(root, path)
		safe, err := repoPath(root, root, rel)
		if err != nil {
			return err
		}
		if kind == "service" {
			v, _, err := readYAML(safe)
			if err != nil {
				return err
			}
			if object(v, "services") != nil || text(v, "compose") != "" {
				kind = "compose"
			}
		}
		found = append(found, match{kind, safe, len(strings.Split(rel, string(filepath.Separator)))})
		return nil
	})
	if err != nil {
		return "", "", err
	}
	if len(found) == 0 {
		return "", "", nil
	}
	slices.SortFunc(found, func(a, b match) int {
		if a.depth != b.depth {
			return a.depth - b.depth
		}
		return strings.Compare(a.path, b.path)
	})
	if len(found) > 1 && found[0].depth == found[1].depth {
		return "", "", fmt.Errorf("multiple Luma deployment manifests found at the same priority")
	}
	return found[0].kind, found[0].path, nil
}
func (s *session) checkout(ctx context.Context, r builddomain.ImageRequest) (string, string, error) {
	dest := filepath.Join(s.path, "src")
	env := s.env()
	for _, key := range []string{"GIT_TRACE", "GIT_TRACE2", "GIT_TRACE_PACKET", "GIT_TRACE_PERFORMANCE", "GIT_TRACE_SETUP", "GIT_TRACE_SHALLOW", "GIT_TRACE_CURL", "GIT_CURL_VERBOSE", "GIT_CONFIG_PARAMETERS"} {
		env[key] = ""
	}
	env["GIT_TERMINAL_PROMPT"] = "0"
	env["GIT_CONFIG_NOSYSTEM"] = "1"
	env["GIT_CONFIG_GLOBAL"] = "/dev/null"
	env["GIT_CONFIG_COUNT"] = "0"
	if r.GitToken != "" {
		username := first(r.GitUsername, "x-access-token")
		for name, value := range map[string]string{"git-username": username, "git-password": r.GitToken} {
			if err := os.WriteFile(filepath.Join(s.path, name), []byte(value), 0600); err != nil {
				return "", "", err
			}
		}
		helper := filepath.Join(s.path, "askpass.sh")
		script := "#!/bin/sh\ncase \"$1\" in\n*Username*|*username*) cat \"$LUMA_GIT_USERNAME_FILE\" ;;\n*) cat \"$LUMA_GIT_PASSWORD_FILE\" ;;\nesac\n"
		if err := os.WriteFile(helper, []byte(script), 0700); err != nil {
			return "", "", err
		}
		env["GIT_ASKPASS"] = helper
		env["GIT_ASKPASS_REQUIRE"] = "force"
		env["LUMA_GIT_USERNAME_FILE"] = filepath.Join(s.path, "git-username")
		env["LUMA_GIT_PASSWORD_FILE"] = filepath.Join(s.path, "git-password")
		defer func() {
			for _, name := range []string{"git-username", "git-password", "askpass.sh"} {
				_ = os.Remove(filepath.Join(s.path, name))
			}
		}()
	} else {
		env["GIT_ASKPASS"] = "/usr/bin/false"
	}
	commands := [][]string{}
	if builddomain.IsFullCommit(r.Ref) {
		commands = [][]string{{"init", "--", dest}, {"-C", dest, "remote", "add", "origin", r.RepoURL}, {"-C", dest, "fetch", "--depth", "1", "--no-tags", "origin", r.Ref}, {"-C", dest, "checkout", "--detach", "FETCH_HEAD"}}
	} else {
		args := []string{"clone", "--depth", "1"}
		if r.Ref != "" {
			args = append(args, "--branch", r.Ref)
		}
		args = append(args, "--", r.RepoURL, dest)
		commands = append(commands, args)
	}
	for _, args := range commands {
		if _, err := s.command(ctx, node.Command{Args: append([]string{s.executor.Git}, args...), Env: env, Timeout: 5 * time.Minute}); err != nil {
			return "", "", err
		}
	}
	out, err := s.command(ctx, node.Command{Args: []string{s.executor.Git, "-C", dest, "rev-parse", "--short", "HEAD"}, Env: env, Timeout: time.Minute})
	if err != nil {
		return "", "", err
	}
	sha := strings.TrimSpace(out)
	if len(sha) < 6 || len(sha) > 64 || strings.Trim(sha, "0123456789abcdef") != "" {
		return "", "", fmt.Errorf("Git returned invalid commit identity")
	}
	return dest, sha, nil
}
