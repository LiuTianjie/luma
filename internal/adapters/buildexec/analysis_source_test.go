package buildexec_test

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"net/http/cgi"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/LiuTianjie/luma/internal/adapters/builderfs"
	"github.com/LiuTianjie/luma/internal/adapters/buildexec"
	"github.com/LiuTianjie/luma/internal/adapters/process"
	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/node"
)

func analysisFixture(t *testing.T) (string, buildexec.AnalysisSource, builddomain.AnalysisRequest) {
	t.Helper()
	base, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	git, err := exec.LookPath("git")
	if err != nil {
		t.Skip("git is unavailable")
	}
	e := buildexec.AnalysisSource{Runner: process.Runner{}, Files: builderfs.Store{Root: filepath.Join(base, "store")}, Git: git, Scratch: filepath.Join(base, "scratch")}
	r := builddomain.AnalysisRequest{BuilderTaskID: "builder-task", ExternalOperationID: "operation", TenantRef: "tenant", ApplicationRef: "application", CredentialLeaseID: "credential-lease", PolicyVersion: "policy-v1", AgentImageDigest: "runner.test/analyzer@sha256:" + strings.Repeat("a", 64), Limits: builddomain.AnalysisLimits{CPU: 1, MemoryMiB: 256, DiskMiB: 256, TimeoutSeconds: 30}}
	return base, e, r
}
func certificate(t *testing.T, base string, server *httptest.Server) string {
	t.Helper()
	name := filepath.Join(base, "ca.pem")
	if err := os.WriteFile(name, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	return name
}
func checkPrepared(t *testing.T, p *buildexec.PreparedAnalysis, secrets ...string) {
	t.Helper()
	entries, err := os.ReadDir(p.Work)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 3 || entries[0].Name() != "input" || entries[1].Name() != "output" || entries[2].Name() != "workspace" {
		t.Fatalf("unexpected analyzer bind tree: %v", entries)
	}
	metadata, err := os.ReadFile(filepath.Join(p.Input, "metadata.json"))
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range secrets {
		if secret != "" && strings.Contains(string(metadata), secret) {
			t.Fatal("secret in analyzer metadata")
		}
	}
	var decoded builddomain.AnalysisMetadata
	if err := json.Unmarshal(metadata, &decoded); err != nil || decoded != p.Metadata {
		t.Fatalf("metadata mismatch: %v", err)
	}
	if p.Metadata.SourceSnapshotDigest != p.Snapshot.Digest || p.Metadata.SourceTreeDigest != p.Snapshot.TreeDigest {
		t.Fatal("unbound source snapshot")
	}
}

func TestAnalysisSourceRealHTTPSGitPinnedCommitAndPrivateCredentials(t *testing.T) {
	base, e, r := analysisFixture(t)
	repo := filepath.Join(base, "repo")
	if err := os.Mkdir(repo, 0700); err != nil {
		t.Fatal(err)
	}
	git := func(args ...string) string {
		t.Helper()
		cmd := exec.Command(e.Git, append([]string{"-C", repo}, args...)...)
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("git %v: %s, %v", args, out, err)
		}
		return strings.TrimSpace(string(out))
	}
	git("init", "--initial-branch=main")
	git("config", "user.email", "fixture@example.test")
	git("config", "user.name", "Fixture")
	if err := os.Mkdir(filepath.Join(repo, "site"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, "site", "index.html"), []byte("pinned source"), 0644); err != nil {
		t.Fatal(err)
	}
	git("add", ".")
	git("commit", "-m", "first")
	commit := git("rev-parse", "HEAD")
	if err := os.WriteFile(filepath.Join(repo, "site", "index.html"), []byte("new default branch"), 0644); err != nil {
		t.Fatal(err)
	}
	git("add", ".")
	git("commit", "-m", "second")
	bare := filepath.Join(base, "repo.git")
	cmd := exec.Command(e.Git, "clone", "--bare", repo, bare)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("bare clone: %s %v", out, err)
	}
	backend := &cgi.Handler{Path: e.Git, Args: []string{"http-backend"}, Root: "/", Env: []string{"GIT_PROJECT_ROOT=" + base, "GIT_HTTP_EXPORT_ALL=1"}}
	var authenticated atomic.Int32
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, q *http.Request) {
		user, password, ok := q.BasicAuth()
		if !ok || user != "robot" || password != "private-source-token" {
			w.Header().Set("WWW-Authenticate", `Basic realm="fixture"`)
			w.WriteHeader(401)
			return
		}
		authenticated.Add(1)
		backend.ServeHTTP(w, q)
	}))
	defer server.Close()
	e.CAFile = certificate(t, base, server)
	r.SourceRef = builddomain.SourceReference{Repository: server.URL + "/repo.git", Ref: commit, Subdirectory: "site"}
	r.GitToken = "private-source-token"
	r.GitUsername = "robot"
	// Inherited Git config and trace environment must not influence this lane.
	t.Setenv("GIT_CONFIG_PARAMETERS", "not valid git config")
	t.Setenv("GIT_TRACE", filepath.Join(base, "leaked-trace"))
	t.Setenv("GIT_SSL_NO_VERIFY", "true")
	var phases []string
	p, err := e.Prepare(context.Background(), r, func(name, status string) { phases = append(phases, name+":"+status) })
	if err != nil {
		t.Fatal(err)
	}
	checkPrepared(t, p, r.GitToken, r.SourceRef.Repository)
	if p.Metadata.ResolvedCommit != commit || authenticated.Load() == 0 {
		t.Fatal("pinned authenticated checkout was not used")
	}
	data, err := os.ReadFile(filepath.Join(p.Workspace, "index.html"))
	if err != nil || string(data) != "pinned source" {
		t.Fatalf("source mismatch: %q %v", data, err)
	}
	if _, err := os.Stat(filepath.Join(p.Workspace, ".git")); !os.IsNotExist(err) {
		t.Fatal("git metadata entered analyzer bind")
	}
	if _, err := os.Stat(filepath.Join(base, "leaked-trace")); !os.IsNotExist(err) {
		t.Fatal("inherited trace was used")
	}
	if strings.Join(phases, ",") != "source_fetch:running,source_fetch:succeeded,source_snapshot:running,source_snapshot:succeeded" {
		t.Fatalf("bad phases: %v", phases)
	}
	store := builderfs.Store{Root: filepath.Join(base, "store")}
	content, err := store.OpenSnapshot(context.Background(), p.Snapshot.Digest, builddomain.SourceLimits{MaxBytes: 256 << 20, MaxEntries: 200000})
	if err != nil {
		t.Fatal(err)
	}
	content.Close()
	work := p.Work
	if err := p.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(work); !os.IsNotExist(err) {
		t.Fatal("workspace not cleaned")
	}
}

func TestAnalysisSourceVerifiedHTTPSObjects(t *testing.T) {
	for _, media := range []string{"text/html", "application/zip"} {
		t.Run(media, func(t *testing.T) {
			base, e, r := analysisFixture(t)
			contents := []byte("<html>uploaded source</html>")
			body := contents
			if media == "application/zip" {
				var b bytes.Buffer
				w := zip.NewWriter(&b)
				entry, _ := w.Create("index.html")
				entry.Write(contents)
				entry, _ = w.Create("assets/中文.txt")
				entry.Write([]byte("static asset"))
				w.Close()
				body = b.Bytes()
			}
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, q *http.Request) { w.Write(body) }))
			defer server.Close()
			e.CAFile = certificate(t, base, server)
			u, _ := url.Parse(server.URL)
			r.SourceRef = builddomain.SourceReference{Kind: "object", Digest: fmt.Sprintf("sha256:%x", sha256.Sum256(body)), MediaType: media, SizeBytes: int64(len(body))}
			r.ObjectURL = server.URL + "/source?signature=private-signed-token"
			r.ObjectAllowedHost = u.Hostname()
			t.Setenv("HTTPS_PROXY", "http://127.0.0.1:1")
			p, err := e.Prepare(context.Background(), r, nil)
			if err != nil {
				t.Fatal(err)
			}
			defer p.Close()
			checkPrepared(t, p, "private-signed-token", r.ObjectURL)
			if got, _ := os.ReadFile(filepath.Join(p.Workspace, "index.html")); !bytes.Equal(got, contents) {
				t.Fatal("wrong uploaded source")
			}
			if p.Metadata.ResolvedCommit != strings.TrimPrefix(r.SourceRef.Digest, "sha256:") {
				t.Fatal("object identity lost")
			}
		})
	}
}

func TestAnalysisObjectFailuresDoNotLeakSignedURLsOrKeepWorkspace(t *testing.T) {
	for _, failure := range []string{"redirect", "digest", "size", "tls"} {
		t.Run(failure, func(t *testing.T) {
			base, e, r := analysisFixture(t)
			body := []byte("uploaded source")
			var redirected atomic.Int32
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, q *http.Request) {
				if q.URL.Path == "/leak" {
					redirected.Add(1)
				}
				if failure == "redirect" && q.URL.Path != "/leak" {
					http.Redirect(w, q, "/leak", http.StatusFound)
					return
				}
				w.Write(body)
			}))
			defer server.Close()
			if failure != "tls" {
				e.CAFile = certificate(t, base, server)
			}
			u, _ := url.Parse(server.URL)
			r.SourceRef = builddomain.SourceReference{Kind: "object", Digest: fmt.Sprintf("sha256:%x", sha256.Sum256(body)), MediaType: "text/html", SizeBytes: int64(len(body))}
			r.ObjectURL = server.URL + "/source?signature=private-signed-token"
			r.ObjectAllowedHost = u.Hostname()
			if failure == "digest" {
				r.SourceRef.Digest = "sha256:" + strings.Repeat("0", 64)
			}
			if failure == "size" {
				r.SourceRef.SizeBytes++
			}
			p, err := e.Prepare(context.Background(), r, nil)
			if err == nil {
				p.Close()
				t.Fatal("invalid source accepted")
			}
			if strings.Contains(err.Error(), "private-signed-token") || strings.Contains(err.Error(), server.URL) {
				t.Fatal("download URL leaked")
			}
			if redirected.Load() != 0 {
				t.Fatal("followed object download redirect")
			}
			entries, err := os.ReadDir(e.Scratch)
			if err != nil || len(entries) != 0 {
				t.Fatalf("failed workspace retained: %v, %v", entries, err)
			}
		})
	}
}

type analysisRunnerFunc func(context.Context, node.Command, func(string)) (node.CommandResult, error)

func (f analysisRunnerFunc) Run(ctx context.Context, c node.Command, emit func(string)) (node.CommandResult, error) {
	return f(ctx, c, emit)
}

func TestAnalysisBudgetCancelsGitAndCleansPrivateScratch(t *testing.T) {
	_, e, r := analysisFixture(t)
	r.SourceRef.Repository = "https://source.test/repo.git"
	e.Runner = analysisRunnerFunc(func(ctx context.Context, c node.Command, _ func(string)) (node.CommandResult, error) {
		if !c.ClearEnv {
			t.Error("source tool inherits process environment")
		}
		destination := c.Args[len(c.Args)-1]
		if err := os.MkdirAll(destination, 0700); err != nil {
			return node.CommandResult{}, err
		}
		f, err := os.Create(filepath.Join(destination, "oversized"))
		if err != nil {
			return node.CommandResult{}, err
		}
		err = f.Truncate((256 << 20) + 1)
		f.Close()
		if err != nil {
			return node.CommandResult{}, err
		}
		select {
		case <-ctx.Done():
			return node.CommandResult{}, ctx.Err()
		case <-time.After(3 * time.Second):
			return node.CommandResult{}, fmt.Errorf("budget watcher did not cancel")
		}
	})
	_, err := e.Prepare(context.Background(), r, nil)
	if err == nil || !strings.Contains(err.Error(), "disk budget") {
		t.Fatalf("wrong budget result: %v", err)
	}
	entries, err := os.ReadDir(e.Scratch)
	if err != nil || len(entries) != 0 {
		t.Fatal("budget failure retained scratch")
	}
}

func TestAnalysisSourceDeadlineInterruptsObjectBody(t *testing.T) {
	base, e, r := analysisFixture(t)
	body := []byte("source body")
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, q *http.Request) {
		w.Header().Set("Content-Length", fmt.Sprint(len(body)))
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		<-q.Context().Done()
	}))
	defer server.Close()
	e.CAFile = certificate(t, base, server)
	u, _ := url.Parse(server.URL)
	r.SourceRef = builddomain.SourceReference{Kind: "object", Digest: fmt.Sprintf("sha256:%x", sha256.Sum256(body)), MediaType: "text/html", SizeBytes: int64(len(body))}
	r.ObjectURL = server.URL + "/source?signature=private"
	r.ObjectAllowedHost = u.Hostname()
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	_, err := e.Prepare(ctx, r, nil)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("body download ignored deadline: %v", err)
	}
	entries, err := os.ReadDir(e.Scratch)
	if err != nil || len(entries) != 0 {
		t.Fatal("deadline failure retained scratch")
	}
}
