package buildexec

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/node"
)

// AnalysisSource prepares the exact persisted tree for the rootless analyzer.
// Files is supplied by the application composition root; adapters stay separate.
type AnalysisSource struct {
	Runner               node.CommandRunner
	Files                builddomain.AnalysisSourceFiles
	Git, Scratch, CAFile string
	MaxEntries           int
}

type PreparedAnalysis struct {
	Work, Workspace, Input, Output string
	Snapshot                       builddomain.Snapshot
	Metadata                       builddomain.AnalysisMetadata
	Deadline                       time.Time
	cleanupBlocked                 atomic.Bool
	once                           sync.Once
	closeErr                       error
}

func (p *PreparedAnalysis) Close() error {
	p.once.Do(func() {
		if p.cleanupBlocked.Load() {
			p.closeErr = fmt.Errorf("analysis workspace retained because runner cleanup was not verified")
			return
		}
		p.closeErr = os.RemoveAll(p.Work)
	})
	return p.closeErr
}

func (p *PreparedAnalysis) markCleanupBlocked() { p.cleanupBlocked.Store(true) }

func (e AnalysisSource) Prepare(ctx context.Context, request builddomain.AnalysisRequest, phase func(string, string)) (prepared *PreparedAnalysis, err error) {
	r, err := request.ValidateSource()
	if err != nil {
		return nil, err
	}
	if e.Files == nil || !filepath.IsAbs(e.Scratch) {
		return nil, fmt.Errorf("analysis source storage is not configured")
	}
	maxEntries := e.MaxEntries
	if maxEntries == 0 {
		maxEntries = 200000
	}
	limits := builddomain.SourceLimits{MaxBytes: r.Limits.DiskMiB << 20, MaxEntries: maxEntries}
	if err := limits.Validate(); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(e.Scratch, 0700); err != nil {
		return nil, err
	}
	info, err := os.Lstat(e.Scratch)
	if err != nil || !info.IsDir() || info.Mode().Perm()&0077 != 0 {
		return nil, fmt.Errorf("analysis scratch must be a private directory")
	}
	if err := analysisFreeDisk(e.Scratch, limits.MaxBytes); err != nil {
		return nil, err
	}
	work, err := os.MkdirTemp(e.Scratch, "analyze-")
	if err != nil {
		return nil, err
	}
	p := &PreparedAnalysis{Work: work, Workspace: filepath.Join(work, "workspace"), Input: filepath.Join(work, "input"), Output: filepath.Join(work, "output"), Deadline: time.Now().Add(time.Duration(r.Limits.TimeoutSeconds) * time.Second)}
	defer func() {
		if err != nil {
			_ = p.Close()
		}
	}()
	if deadline, ok := ctx.Deadline(); ok && deadline.Before(p.Deadline) {
		p.Deadline = deadline
	}
	ctx, timeoutCancel := context.WithDeadline(ctx, p.Deadline)
	defer timeoutCancel()
	ctx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	stop := watchAnalysisBudget(ctx, cancel, work, limits)
	defer stop()
	emit := func(name, status string) {
		if phase != nil {
			phase(name, status)
		}
	}
	checkout := filepath.Join(work, "checkout")
	sourceObject := filepath.Join(work, "source.object")
	archive := filepath.Join(work, "source.tar")
	emit("source_fetch", "running")
	resolved, sourceRoot := "", checkout
	if r.SourceRef.Kind == "object" {
		if err := e.downloadObject(ctx, r, sourceObject); err != nil {
			return nil, analysisCause(ctx, err)
		}
		if err := e.Files.MaterializeObject(ctx, sourceObject, checkout, r.SourceRef.MediaType, limits); err != nil {
			return nil, analysisCause(ctx, err)
		}
		resolved = strings.TrimPrefix(r.SourceRef.Digest, "sha256:")
	} else {
		resolved, err = e.cloneAnalysis(ctx, r, work, checkout)
		if err != nil {
			return nil, analysisCause(ctx, err)
		}
		if r.SourceRef.Subdirectory != "" {
			sourceRoot, err = repoPath(checkout, checkout, r.SourceRef.Subdirectory)
			if err != nil {
				return nil, fmt.Errorf("analysis subdirectory does not exist inside the repository")
			}
			info, err := os.Stat(sourceRoot)
			if err != nil || !info.IsDir() {
				return nil, fmt.Errorf("analysis source subdirectory is not a directory")
			}
		}
	}
	if err := analysisBudget(ctx, work, limits); err != nil {
		return nil, err
	}
	emit("source_fetch", "succeeded")
	emit("source_snapshot", "running")
	p.Snapshot, err = e.Files.Capture(ctx, sourceRoot, archive, limits)
	if err != nil {
		return nil, analysisCause(ctx, err)
	}
	if err := analysisBudget(ctx, work, limits); err != nil {
		return nil, err
	}
	input, err := os.Open(archive)
	if err != nil {
		return nil, err
	}
	err = e.Files.PutSnapshot(ctx, input, p.Snapshot.Digest, limits)
	input.Close()
	if err != nil {
		return nil, analysisCause(ctx, err)
	}
	// Drop the Git database and mutable checkout before creating the runner view.
	if err := os.RemoveAll(checkout); err != nil {
		return nil, err
	}
	if p.Snapshot.ArchiveBytes > limits.MaxBytes-p.Snapshot.SourceBytes {
		return nil, fmt.Errorf("analysis snapshot materialization exceeds disk budget")
	}
	input, err = os.Open(archive)
	if err != nil {
		return nil, err
	}
	err = e.Files.Materialize(ctx, input, p.Workspace, p.Snapshot.Digest, limits)
	input.Close()
	if err != nil {
		return nil, analysisCause(ctx, err)
	}
	if err := os.Remove(archive); err != nil {
		return nil, err
	}
	snapshotID, err := builddomain.SnapshotID(r.BuilderTaskID, p.Snapshot.Digest)
	if err != nil {
		return nil, err
	}
	p.Metadata = builddomain.AnalysisMetadata{SchemaVersion: "lae.agent-analysis-metadata/v1", BuilderTaskID: r.BuilderTaskID, ExternalOperationID: r.ExternalOperationID, TenantRef: r.TenantRef, ApplicationRef: r.ApplicationRef, ResolvedCommit: resolved, SourceTreeDigest: p.Snapshot.TreeDigest, SourceSnapshotID: snapshotID, SourceSnapshotDigest: p.Snapshot.Digest, PolicyVersion: r.PolicyVersion, AgentImageDigest: r.AgentImageDigest}
	for _, directory := range []string{p.Input, p.Output} {
		if err := os.Mkdir(directory, 0700); err != nil {
			return nil, err
		}
	}
	metadata, err := json.Marshal(p.Metadata)
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(filepath.Join(p.Input, "metadata.json"), metadata, 0600); err != nil {
		return nil, err
	}
	if err := analysisBudget(ctx, work, limits); err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, analysisCause(ctx, err)
	}
	emit("source_snapshot", "succeeded")
	return p, nil
}

func analysisCause(ctx context.Context, err error) error {
	if cause := context.Cause(ctx); cause != nil {
		return cause
	}
	return err
}

func analysisBudget(ctx context.Context, work string, limits builddomain.SourceLimits) error {
	var total int64
	count := 0
	return filepath.WalkDir(work, func(name string, entry fs.DirEntry, err error) error {
		if err := ctx.Err(); err != nil {
			return analysisCause(ctx, err)
		}
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		count++
		if count > limits.MaxEntries*4 {
			return fmt.Errorf("analysis workspace has too many files")
		}
		if entry.IsDir() || entry.Type()&os.ModeSymlink != 0 {
			return nil
		}
		info, err := entry.Info()
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("analysis workspace contains a special file")
		}
		if info.Size() > limits.MaxBytes-total {
			return fmt.Errorf("analysis workspace exceeds disk budget")
		}
		total += info.Size()
		return nil
	})
}

func watchAnalysisBudget(ctx context.Context, cancel context.CancelCauseFunc, work string, limits builddomain.SourceLimits) func() {
	stop, done := make(chan struct{}), make(chan struct{})
	go func() {
		defer close(done)
		ticker := time.NewTicker(250 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-stop:
				return
			case <-ticker.C:
				if err := analysisBudget(ctx, work, limits); err != nil {
					cancel(err)
					return
				}
			}
		}
	}()
	return func() { close(stop); <-done }
}

func (e AnalysisSource) cloneAnalysis(ctx context.Context, r builddomain.AnalysisRequest, work, destination string) (string, error) {
	if e.Runner == nil || e.Git == "" {
		return "", fmt.Errorf("analysis Git runtime is not configured")
	}
	auth, err := os.MkdirTemp(work, "git-auth-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(auth)
	env := map[string]string{"PATH": os.Getenv("PATH"), "HOME": auth, "XDG_CONFIG_HOME": filepath.Join(auth, "xdg"), "LANG": "C", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_COUNT": "0", "GIT_ASKPASS": "/usr/bin/false", "GIT_ALLOW_PROTOCOL": "https"}
	if e.CAFile != "" {
		env["GIT_SSL_CAINFO"] = e.CAFile
	}
	if r.GitToken != "" {
		username := r.GitUsername
		if username == "" {
			username = "x-access-token"
		}
		for name, value := range map[string]string{"username": username, "password": r.GitToken} {
			if err := os.WriteFile(filepath.Join(auth, name), []byte(value), 0600); err != nil {
				return "", err
			}
		}
		helper := filepath.Join(auth, "askpass.sh")
		if err := os.WriteFile(helper, []byte("#!/bin/sh\ncase \"$1\" in\n*Username*|*username*) cat \"$LUMA_GIT_USERNAME_FILE\" ;;\n*) cat \"$LUMA_GIT_PASSWORD_FILE\" ;;\nesac\n"), 0700); err != nil {
			return "", err
		}
		env["GIT_ASKPASS"], env["GIT_ASKPASS_REQUIRE"] = helper, "force"
		env["LUMA_GIT_USERNAME_FILE"], env["LUMA_GIT_PASSWORD_FILE"] = filepath.Join(auth, "username"), filepath.Join(auth, "password")
	}
	base := []string{e.Git, "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never", "-c", "http.followRedirects=false"}
	run := func(args ...string) (string, error) {
		out, err := e.Runner.Run(ctx, node.Command{Args: append(append([]string{}, base...), args...), Env: env, ClearEnv: true, Secrets: []string{r.GitToken}, Timeout: time.Until(deadlineOr(ctx, time.Now().Add(time.Hour)))}, nil)
		if err != nil || out.Code != 0 {
			return "", analysisCause(ctx, fmt.Errorf("analysis Git command failed"))
		}
		return strings.TrimSpace(out.Output), nil
	}
	var commands [][]string
	if builddomain.IsFullCommit(r.SourceRef.Ref) {
		commands = [][]string{{"init", "--", destination}, {"-C", destination, "remote", "add", "origin", r.SourceRef.Repository}, {"-C", destination, "fetch", "--depth", "1", "--no-tags", "origin", r.SourceRef.Ref}, {"-C", destination, "checkout", "--detach", "FETCH_HEAD"}}
	} else {
		args := []string{"clone", "--depth", "1", "--no-tags", "--single-branch"}
		if r.SourceRef.Ref != "" {
			args = append(args, "--branch", r.SourceRef.Ref)
		}
		commands = [][]string{append(args, "--", r.SourceRef.Repository, destination)}
	}
	for _, args := range commands {
		if _, err := run(args...); err != nil {
			return "", err
		}
	}
	commit, err := run("-C", destination, "rev-parse", "HEAD")
	if err != nil {
		return "", err
	}
	if !builddomain.IsFullCommit(commit) {
		return "", fmt.Errorf("analysis Git returned an invalid commit identity")
	}
	return strings.ToLower(commit), nil
}

func deadlineOr(ctx context.Context, fallback time.Time) time.Time {
	if d, ok := ctx.Deadline(); ok {
		return d
	}
	return fallback
}

func (e AnalysisSource) downloadObject(ctx context.Context, r builddomain.AnalysisRequest, destination string) error {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.DisableCompression = true
	defer transport.CloseIdleConnections()
	if e.CAFile != "" {
		pem, err := os.ReadFile(e.CAFile)
		if err != nil {
			return fmt.Errorf("analysis CA file is unavailable")
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return fmt.Errorf("analysis CA file is invalid")
		}
		transport.TLSClientConfig = &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
	}
	client := http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("redirect refused") }}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, r.ObjectURL, nil)
	if err != nil {
		return fmt.Errorf("invalid uploaded source request")
	}
	req.Header.Set("Accept", "application/octet-stream")
	req.Header.Set("User-Agent", "luma-builder/1")
	response, err := client.Do(req)
	if err != nil {
		return analysisCause(ctx, fmt.Errorf("uploaded source download failed"))
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK || (response.ContentLength >= 0 && response.ContentLength != r.SourceRef.SizeBytes) {
		return fmt.Errorf("uploaded source descriptor changed")
	}
	output, err := os.OpenFile(destination, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer output.Close()
	hash := sha256.New()
	n, err := io.Copy(io.MultiWriter(output, hash), io.LimitReader(response.Body, r.SourceRef.SizeBytes+1))
	if err != nil {
		return analysisCause(ctx, fmt.Errorf("uploaded source download failed"))
	}
	if n != r.SourceRef.SizeBytes || fmt.Sprintf("sha256:%x", hash.Sum(nil)) != r.SourceRef.Digest {
		return fmt.Errorf("uploaded source descriptor changed")
	}
	return output.Sync()
}
