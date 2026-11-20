package main

import (
	"context"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/liutianjie/luma-eye/internal/config"
	"github.com/liutianjie/luma-eye/internal/observe"
	"github.com/liutianjie/luma-eye/internal/pipeline"
	"github.com/liutianjie/luma-eye/internal/project"
	"github.com/liutianjie/luma-eye/internal/server"
	"github.com/liutianjie/luma-eye/internal/skillasset"
	"github.com/liutianjie/luma-eye/internal/sourcemap"
)

func TestLoginPersistsConfigAndPowersCLIWithoutEnvVars(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{
		Mode:          config.ModeAll,
		ListenAddr:    ":0",
		DataDir:       dir,
		AdminToken:    "cli-secret-token",
		IPSalt:        "salt",
		BatchInterval: 50 * time.Millisecond,
		BatchSize:     100,
		QueueCapacity: 64,
	}
	store, err := project.NewStore(dir, false)
	if err != nil {
		t.Fatal(err)
	}
	sym, err := sourcemap.NewSymbolicator(dir, 32)
	if err != nil {
		t.Fatal(err)
	}
	metrics := observe.NewMetrics()
	batcher, err := pipeline.NewBatcher(cfg, store, nil, sym, nil, metrics)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	batcher.Start(ctx)
	defer func() {
		cancel()
		shutCtx, sCancel := context.WithTimeout(context.Background(), time.Second)
		batcher.Shutdown(shutCtx)
		sCancel()
	}()

	srv := server.New(cfg, store, nil, batcher, sym, metrics)
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	cfgPath := filepath.Join(t.TempDir(), "luma-eye", "config.json")
	t.Setenv("LUMA_EYE_CONFIG", cfgPath)
	t.Setenv("LUMA_EYE_URL", "")
	t.Setenv("LUMA_EYE_ADMIN_TOKEN", "")

	// Wrong token must fail login verification.
	if err := runLogin([]string{"--url", ts.URL, "--token", "wrong-token"}); err == nil {
		t.Fatalf("expected login failure with wrong token")
	}

	// Valid token saves 0600 config.
	if err := runLogin([]string{"--url", ts.URL, "--token", "cli-secret-token", "--json"}); err != nil {
		t.Fatalf("runLogin failed: %v", err)
	}
	info, err := os.Stat(cfgPath)
	if err != nil {
		t.Fatalf("expected saved config file at %s: %v", cfgPath, err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("expected config permission 0600, got %o", perm)
	}

	// Subsequent commands require zero --url / --token flags or env vars.
	if err := runStatus([]string{"--json"}); err != nil {
		t.Fatalf("runStatus failed: %v", err)
	}
	if err := runProjects([]string{"ensure", "demoapp", "--origins", "https://demo.example.com", "--json"}); err != nil {
		t.Fatalf("runProjects ensure failed: %v", err)
	}
	if _, ok := store.Get("demoapp"); !ok {
		t.Fatalf("expected demoapp project to be provisioned")
	}
	if err := runProjects([]string{"list", "--json"}); err != nil {
		t.Fatalf("runProjects list failed: %v", err)
	}
	if err := runProjects([]string{"delete", "demoapp", "--json"}); err != nil {
		t.Fatalf("runProjects delete failed: %v", err)
	}
	if _, ok := store.Get("demoapp"); ok {
		t.Fatalf("expected demoapp project to be deleted")
	}

	if err := runLogout([]string{"--json"}); err != nil {
		t.Fatalf("runLogout failed: %v", err)
	}
	if _, err := os.Stat(cfgPath); !os.IsNotExist(err) {
		t.Fatalf("expected saved config removed after logout, stat err=%v", err)
	}
}

func TestSkillsInstallAndStatus(t *testing.T) {
	baseDir := t.TempDir()
	if err := runSkills([]string{"install", "--path", baseDir, "--json"}); err != nil {
		t.Fatalf("runSkills install failed: %v", err)
	}
	dst := filepath.Join(baseDir, "luma-eye", "SKILL.md")
	got, err := os.ReadFile(dst)
	if err != nil {
		t.Fatalf("expected installed skill at %s: %v", dst, err)
	}
	if string(got) != string(skillasset.SkillMarkdown) {
		t.Fatalf("installed SKILL.md mismatch: len %d vs %d", len(got), len(skillasset.SkillMarkdown))
	}
	if err := runSkills([]string{"status", "--path", baseDir, "--json"}); err != nil {
		t.Fatalf("runSkills status failed: %v", err)
	}
}
