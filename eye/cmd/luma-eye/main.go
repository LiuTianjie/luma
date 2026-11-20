package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
	_ "time/tzdata"

	"github.com/liutianjie/luma-eye/internal/alert"
	"github.com/liutianjie/luma-eye/internal/clickhouse"
	"github.com/liutianjie/luma-eye/internal/config"
	"github.com/liutianjie/luma-eye/internal/observe"
	"github.com/liutianjie/luma-eye/internal/pipeline"
	"github.com/liutianjie/luma-eye/internal/project"
	"github.com/liutianjie/luma-eye/internal/retention"
	"github.com/liutianjie/luma-eye/internal/server"
	"github.com/liutianjie/luma-eye/internal/skillasset"
	"github.com/liutianjie/luma-eye/internal/sourcemap"
)

func main() {
	if len(os.Args) < 2 {
		printUsage()
		os.Exit(1)
	}

	sub := os.Args[1]
	args := os.Args[2:]
	var err error

	switch sub {
	case "serve":
		err = runServe(args)
	case "login":
		err = runLogin(args)
	case "logout":
		err = runLogout(args)
	case "status":
		err = runStatus(args)
	case "skills":
		err = runSkills(args)
	case "projects":
		err = runProjects(args)
	case "stats":
		err = runStats(args)
	case "funnel":
		err = runFunnel(args)
	case "retention":
		err = runRetention(args)
	case "issues":
		err = runIssues(args)
	case "logs":
		err = runLogs(args)
	case "sourcemaps":
		err = runSourceMaps(args)
	case "grafana":
		err = runGrafana(args)
	case "healthcheck":
		err = runHealthcheck(args)
	case "-h", "--help", "help":
		printUsage()
		return
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand: %s\n\n", sub)
		printUsage()
		os.Exit(1)
	}

	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}

func printUsage() {
	fmt.Print(`luma-eye — Multi-service telemetry, error tracking & product analytics for Luma

Usage:
  luma-eye serve [--mode all|edge|core] [--listen :8090]
  luma-eye login --url https://eye.example.com --token <admin-token> [--skip-verify] [--json]
  luma-eye logout
  luma-eye status [--json]
  luma-eye skills install [--agent all|claude|antigravity|codex] [--path <dir>] [--json]
  luma-eye skills status [--json]
  luma-eye projects list [--json]
  luma-eye projects ensure <slug> [--name ...] [--origins https://app.example.com] [--json]
  luma-eye projects delete <slug> [--json]
  luma-eye stats <project> [--since 24h] [--json]
  luma-eye funnel <project> --steps "pageview,signup,checkout" [--window 3600] [--since 7d] [--json]
  luma-eye retention <project> [--days 7] [--json]
  luma-eye issues <project> [--since 7d] [--status open|resolved|ignored|all] [--detail <fingerprint>] [--resolve <fp>] [--ignore <fp>] [--reopen <fp>] [--json]
  luma-eye logs <project> [--level error] [--search "..."] [--trace <id>] [--since 1h] [--limit 100] [--json]
  luma-eye sourcemaps upload --project <slug> --release <version> [--prefix /assets] <dir-or-file>
  luma-eye grafana render --project <slug> [--datasource victoria|clickhouse] [--output <path>]

Credentials resolution order:
  1. --url / --token flags
  2. LUMA_EYE_URL / LUMA_EYE_ADMIN_TOKEN environment variables
  3. Saved CLI config (~/.config/luma-eye/config.json or $LUMA_EYE_CONFIG)
`)
}

func runServe(args []string) error {
	cfg, err := config.LoadFromEnv()
	if err != nil {
		return err
	}
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	modeFlag := fs.String("mode", string(cfg.Mode), "server mode: all, edge, or core")
	listenFlag := fs.String("listen", cfg.ListenAddr, "HTTP listen address")
	dataFlag := fs.String("data-dir", cfg.DataDir, "data directory")
	chFlag := fs.String("clickhouse-url", cfg.ClickHouseURL, "ClickHouse HTTP URL")
	coreFlag := fs.String("core-url", cfg.CoreURL, "Core server URL (edge mode)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg.Mode = config.Mode(strings.ToLower(strings.TrimSpace(*modeFlag)))
	cfg.ListenAddr = *listenFlag
	cfg.DataDir = *dataFlag
	cfg.ClickHouseURL = strings.TrimRight(*chFlag, "/")
	cfg.CoreURL = strings.TrimRight(*coreFlag, "/")

	if cfg.Mode == config.ModeEdge && cfg.CoreURL == "" {
		return fmt.Errorf("--core-url or LUMA_EYE_CORE_URL is required in edge mode")
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	store, err := project.NewStore(cfg.DataDir, false)
	if err != nil {
		return fmt.Errorf("init project store: %w", err)
	}

	metrics := observe.NewMetrics()
	var chClient *clickhouse.Client
	var symbolicator *sourcemap.Symbolicator
	var notifier *alert.FeishuNotifier

	if cfg.Mode != config.ModeEdge {
		chClient, err = clickhouse.NewClient(cfg.ClickHouseURL, cfg.ClickHouseDB, cfg.ClickHouseUser, cfg.ClickHousePassword)
		if err != nil {
			return fmt.Errorf("init clickhouse client: %w", err)
		}
		chClient.SetRetentionDays(cfg.RetentionDays)
		if err := ensureClickHouseReady(ctx, chClient); err != nil {
			return err
		}
		symbolicator, err = sourcemap.NewSymbolicator(cfg.DataDir, 4096)
		if err != nil {
			return fmt.Errorf("init symbolicator: %w", err)
		}
		notifier = alert.NewFeishuNotifier(cfg.FeishuWebhookURL, cfg.PublicURL)
	}

	batcher, err := pipeline.NewBatcher(cfg, store, chClient, symbolicator, notifier, metrics)
	if err != nil {
		return fmt.Errorf("init batcher: %w", err)
	}
	batcher.Start(ctx)

	var storageBackend retention.StorageBackend
	if chClient != nil {
		storageBackend = chClient
	}
	reaper := retention.NewReaper(cfg, storageBackend, store, symbolicator)
	reaper.Start(ctx)

	if otlp := observe.NewOTLPPusher(cfg.OtelEndpoint, cfg.OtelHeaders, cfg.OtelServiceName, cfg.OtelResourceAttributes, metrics); otlp != nil {
		otlp.Start(ctx, 30*time.Second)
	}

	srv := server.New(cfg, store, chClient, batcher, symbolicator, metrics)
	srv.SetRetentionProvider(reaper.Snapshot)
	httpServer := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		log.Printf("[luma-eye] listening on %s (mode=%s)", cfg.ListenAddr, cfg.Mode)
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
		close(errCh)
	}()

	select {
	case <-ctx.Done():
		log.Printf("[luma-eye] shutting down gracefully...")
		shutCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		_ = httpServer.Shutdown(shutCtx)
		batcher.Shutdown(shutCtx)
		cancel()
		return nil
	case err := <-errCh:
		return err
	}
}

func ensureClickHouseReady(ctx context.Context, ch *clickhouse.Client) error {
	deadline := time.Now().Add(180 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		pingCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
		err := ch.Ping(pingCtx)
		cancel()
		if err == nil {
			schemaCtx, sCancel := context.WithTimeout(ctx, 15*time.Second)
			err = ch.EnsureSchema(schemaCtx)
			sCancel()
			if err == nil {
				return nil
			}
		}
		lastErr = err
		log.Printf("[luma-eye] waiting for ClickHouse (%v)...", err)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(1 * time.Second):
		}
	}
	return fmt.Errorf("clickhouse unavailable after 180s: %w", lastErr)
}

type cliSavedConfig struct {
	URL   string `json:"url"`
	Token string `json:"token"`
}

func cliConfigPath() string {
	if p := strings.TrimSpace(os.Getenv("LUMA_EYE_CONFIG")); p != "" {
		return p
	}
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return ""
	}
	return filepath.Join(home, ".config", "luma-eye", "config.json")
}

func loadSavedCLIConfig() cliSavedConfig {
	p := cliConfigPath()
	if p == "" {
		return cliSavedConfig{}
	}
	raw, err := os.ReadFile(p)
	if err != nil {
		return cliSavedConfig{}
	}
	var cfg cliSavedConfig
	_ = json.Unmarshal(raw, &cfg)
	return cfg
}

func saveCLIConfig(cfg cliSavedConfig) (string, error) {
	p := cliConfigPath()
	if p == "" {
		return "", fmt.Errorf("cannot determine home directory for luma-eye config")
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return "", err
	}
	raw, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return "", err
	}
	raw = append(raw, '\n')
	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return "", err
	}
	if err := os.Rename(tmp, p); err != nil {
		_ = os.Remove(tmp)
		return "", err
	}
	return p, nil
}

type apiClient struct {
	baseURL string
	token   string
	http    *http.Client
}

func newAPIClient(baseURL, token string) *apiClient {
	saved := loadSavedCLIConfig()
	if baseURL == "" {
		baseURL = strings.TrimSpace(os.Getenv("LUMA_EYE_URL"))
	}
	if baseURL == "" {
		baseURL = strings.TrimSpace(saved.URL)
	}
	if baseURL == "" {
		baseURL = "http://127.0.0.1:8090"
	}
	if token == "" {
		token = strings.TrimSpace(os.Getenv("LUMA_EYE_ADMIN_TOKEN"))
	}
	if token == "" {
		token = strings.TrimSpace(saved.Token)
	}
	return &apiClient{
		baseURL: strings.TrimRight(baseURL, "/"),
		token:   token,
		http:    &http.Client{Timeout: 30 * time.Second},
	}
}

func (c *apiClient) doJSON(method, path string, reqBody any, out any) ([]byte, error) {
	var bodyReader io.Reader
	if reqBody != nil {
		b, err := json.Marshal(reqBody)
		if err != nil {
			return nil, err
		}
		bodyReader = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, c.baseURL+path, bodyReader)
	if err != nil {
		return nil, err
	}
	if reqBody != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 300 {
		return raw, fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	if out != nil {
		if err := json.Unmarshal(raw, out); err != nil {
			return raw, err
		}
	}
	return raw, nil
}

func addCommonFlags(fs *flag.FlagSet) (urlFlag *string, tokenFlag *string, jsonFlag *bool) {
	urlFlag = fs.String("url", "", "luma-eye server URL")
	tokenFlag = fs.String("token", "", "luma-eye admin token")
	jsonFlag = fs.Bool("json", false, "output raw JSON")
	return
}

func reorderArgs(args []string, valuedFlags ...string) []string {
	valued := make(map[string]bool, len(valuedFlags))
	for _, vf := range valuedFlags {
		valued[vf] = true
	}
	var flags, pos []string
	for i := 0; i < len(args); i++ {
		a := args[i]
		if strings.HasPrefix(a, "-") && a != "-" {
			flags = append(flags, a)
			name := strings.TrimLeft(a, "-")
			if !strings.Contains(name, "=") && valued[name] && i+1 < len(args) {
				i++
				flags = append(flags, args[i])
			}
		} else {
			pos = append(pos, a)
		}
	}
	return append(flags, pos...)
}

func runProjects(args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: luma-eye projects <list|ensure> ...")
	}
	action := args[0]
	rest := args[1:]

	fs := flag.NewFlagSet("projects", flag.ContinueOnError)
	u, t, asJSON := addCommonFlags(fs)
	nameFlag := fs.String("name", "", "display name")
	originsFlag := fs.String("origins", "", "comma-separated allowed origins")
	rpsFlag := fs.Int("rps", 200, "per-project rate limit RPS")
	if err := fs.Parse(reorderArgs(rest, "url", "token", "name", "origins", "rps")); err != nil {
		return err
	}
	cli := newAPIClient(*u, *t)

	switch action {
	case "list":
		raw, err := cli.doJSON(http.MethodGet, "/api/v1/projects", nil, nil)
		if err != nil {
			return err
		}
		if *asJSON {
			fmt.Println(string(raw))
			return nil
		}
		var res struct {
			Projects []struct {
				DSNID          int      `json:"dsn_id"`
				Slug           string   `json:"slug"`
				Name           string   `json:"name"`
				PublicKey      string   `json:"public_key"`
				AllowedOrigins []string `json:"allowed_origins"`
			} `json:"projects"`
		}
		_ = json.Unmarshal(raw, &res)
		for _, p := range res.Projects {
			fmt.Printf("#%d  %-20s  key=%s  origins=%s\n", p.DSNID, p.Slug, p.PublicKey, strings.Join(p.AllowedOrigins, ","))
		}
		return nil
	case "ensure":
		if fs.NArg() < 1 {
			return fmt.Errorf("usage: luma-eye projects ensure <slug>")
		}
		slug := fs.Arg(0)
		var origins []string
		if *originsFlag != "" {
			for _, o := range strings.Split(*originsFlag, ",") {
				if o = strings.TrimSpace(o); o != "" {
					origins = append(origins, o)
				}
			}
		}
		req := map[string]any{
			"slug":            slug,
			"name":            *nameFlag,
			"allowed_origins": origins,
			"rate_limit_rps":  *rpsFlag,
		}
		raw, err := cli.doJSON(http.MethodPost, "/api/v1/projects", req, nil)
		if err != nil {
			return err
		}
		if *asJSON {
			fmt.Println(string(raw))
			return nil
		}
		var res struct {
			Created   bool   `json:"created"`
			IngestURL string `json:"ingest_url"`
			SentryDSN string `json:"sentry_dsn"`
			Project   struct {
				DSNID     int    `json:"dsn_id"`
				Slug      string `json:"slug"`
				PublicKey string `json:"public_key"`
			} `json:"project"`
		}
		_ = json.Unmarshal(raw, &res)
		fmt.Printf("project=%s (created=%v, dsn_id=%d)\npublic_key=%s\ningest_url=%s\nsentry_dsn=%s\n",
			res.Project.Slug, res.Created, res.Project.DSNID, res.Project.PublicKey, res.IngestURL, res.SentryDSN)
		return nil
	case "delete":
		if fs.NArg() < 1 {
			return fmt.Errorf("usage: luma-eye projects delete <slug>")
		}
		slug := fs.Arg(0)
		raw, err := cli.doJSON(http.MethodDelete, "/api/v1/projects/"+url.PathEscape(slug), nil, nil)
		if err != nil {
			return err
		}
		if *asJSON {
			fmt.Println(string(raw))
			return nil
		}
		fmt.Printf("deleted project: %s\n", slug)
		return nil
	default:
		return fmt.Errorf("unknown projects subcommand %q", action)
	}
}

func runLogin(args []string) error {
	fs := flag.NewFlagSet("login", flag.ContinueOnError)
	u, t, asJSON := addCommonFlags(fs)
	skipVerify := fs.Bool("skip-verify", false, "save credentials without verifying against /api/v1/auth/status")
	if err := fs.Parse(reorderArgs(args, "url", "token")); err != nil {
		return err
	}
	cleanURL := strings.TrimRight(strings.TrimSpace(*u), "/")
	if cleanURL == "" {
		cleanURL = strings.TrimRight(strings.TrimSpace(os.Getenv("LUMA_EYE_URL")), "/")
	}
	cleanToken := strings.TrimSpace(*t)
	if cleanToken == "" {
		cleanToken = strings.TrimSpace(os.Getenv("LUMA_EYE_ADMIN_TOKEN"))
	}
	if cleanURL == "" || cleanToken == "" {
		return fmt.Errorf("usage: luma-eye login --url https://eye.example.com --token <admin-token>")
	}

	mode := ""
	if !*skipVerify {
		cli := &apiClient{
			baseURL: cleanURL,
			token:   cleanToken,
			http:    &http.Client{Timeout: 15 * time.Second},
		}
		var authRes struct {
			Authenticated bool   `json:"authenticated"`
			Mode          string `json:"mode"`
		}
		if _, err := cli.doJSON(http.MethodGet, "/api/v1/auth/status", nil, &authRes); err != nil {
			return fmt.Errorf("verify server %s: %w", cleanURL, err)
		}
		if !authRes.Authenticated {
			return fmt.Errorf("authentication failed: invalid admin token for %s", cleanURL)
		}
		mode = authRes.Mode
	}

	cfgPath, err := saveCLIConfig(cliSavedConfig{URL: cleanURL, Token: cleanToken})
	if err != nil {
		return err
	}
	if *asJSON {
		out, _ := json.MarshalIndent(map[string]any{
			"ok":          true,
			"url":         cleanURL,
			"mode":        mode,
			"config_path": cfgPath,
			"verified":    !*skipVerify,
		}, "", "  ")
		fmt.Println(string(out))
		return nil
	}
	fmt.Printf("Logged in to %s (mode=%s). Saved credentials to %s\n", cleanURL, mode, cfgPath)
	return nil
}

func runLogout(args []string) error {
	fs := flag.NewFlagSet("logout", flag.ContinueOnError)
	asJSON := fs.Bool("json", false, "output JSON")
	if err := fs.Parse(args); err != nil {
		return err
	}
	p := cliConfigPath()
	if p != "" {
		if err := os.Remove(p); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	if *asJSON {
		fmt.Printf("{\"ok\":true,\"removed\":%q}\n", p)
		return nil
	}
	fmt.Printf("Cleared saved luma-eye credentials (%s)\n", p)
	return nil
}

func runStatus(args []string) error {
	fs := flag.NewFlagSet("status", flag.ContinueOnError)
	u, t, asJSON := addCommonFlags(fs)
	if err := fs.Parse(reorderArgs(args, "url", "token")); err != nil {
		return err
	}
	cli := newAPIClient(*u, *t)
	cfgPath := cliConfigPath()
	_, statErr := os.Stat(cfgPath)
	hasSaved := cfgPath != "" && statErr == nil

	var authRes struct {
		Authenticated bool              `json:"authenticated"`
		Mode          string            `json:"mode"`
		Retention     *retention.Status `json:"retention,omitempty"`
	}
	_, err := cli.doJSON(http.MethodGet, "/api/v1/auth/status", nil, &authRes)
	if err != nil {
		return err
	}

	projectCount := 0
	if authRes.Authenticated {
		var projRes struct {
			Projects []json.RawMessage `json:"projects"`
		}
		if _, err := cli.doJSON(http.MethodGet, "/api/v1/projects", nil, &projRes); err == nil {
			projectCount = len(projRes.Projects)
		}
	}

	if *asJSON {
		payload := map[string]any{
			"url":              cli.baseURL,
			"authenticated":    authRes.Authenticated,
			"mode":             authRes.Mode,
			"project_count":    projectCount,
			"has_saved_config": hasSaved,
			"config_path":      cfgPath,
		}
		if authRes.Retention != nil {
			payload["retention"] = authRes.Retention
		}
		out, _ := json.MarshalIndent(payload, "", "  ")
		fmt.Println(string(out))
		return nil
	}
	fmt.Printf("URL: %s | Mode: %s | Authenticated: %v | Projects: %d",
		cli.baseURL, authRes.Mode, authRes.Authenticated, projectCount)
	if authRes.Retention != nil {
		usedMiB := float64(authRes.Retention.TotalBytes) / (1024 * 1024)
		if authRes.Retention.MaxStorageBytes > 0 {
			capGiB := float64(authRes.Retention.MaxStorageBytes) / float64(int64(1)<<30)
			fmt.Printf(" | Retention: %dd (%.1f MiB / %.1f GiB)", authRes.Retention.RetentionDays, usedMiB, capGiB)
		} else {
			fmt.Printf(" | Retention: %dd (%.1f MiB / unlimited)", authRes.Retention.RetentionDays, usedMiB)
		}
	}
	fmt.Println()
	return nil
}

func resolveSkillDirs(agent, customPath string) ([]string, error) {
	if customPath != "" {
		return []string{customPath}, nil
	}
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return nil, fmt.Errorf("cannot determine home directory")
	}
	switch strings.ToLower(strings.TrimSpace(agent)) {
	case "", "all":
		dirs := []string{
			filepath.Join(home, ".agents", "skills"),
			filepath.Join(home, ".claude", "skills"),
			filepath.Join(home, ".gemini", "config", "skills"),
		}
		if _, err := os.Stat(filepath.Join(home, ".codex")); err == nil {
			dirs = append(dirs, filepath.Join(home, ".codex", "skills"))
		}
		return dirs, nil
	case "claude":
		return []string{filepath.Join(home, ".claude", "skills")}, nil
	case "antigravity", "gemini":
		return []string{filepath.Join(home, ".gemini", "config", "skills"), filepath.Join(home, ".agents", "skills")}, nil
	case "codex":
		return []string{filepath.Join(home, ".codex", "skills"), filepath.Join(home, ".agents", "skills")}, nil
	default:
		return nil, fmt.Errorf("unknown --agent %q (must be all, claude, antigravity, or codex)", agent)
	}
}

func runSkills(args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: luma-eye skills <install|status> ...")
	}
	action := args[0]
	rest := args[1:]

	switch action {
	case "install":
		fs := flag.NewFlagSet("skills install", flag.ContinueOnError)
		agent := fs.String("agent", "all", "target agent: all|claude|antigravity|codex")
		customPath := fs.String("path", "", "custom skills base directory")
		asJSON := fs.Bool("json", false, "output JSON")
		if err := fs.Parse(reorderArgs(rest, "agent", "path")); err != nil {
			return err
		}
		dirs, err := resolveSkillDirs(*agent, *customPath)
		if err != nil {
			return err
		}
		var installed []string
		for _, base := range dirs {
			dst := filepath.Join(base, "luma-eye", "SKILL.md")
			if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
				return err
			}
			if err := os.WriteFile(dst, skillasset.SkillMarkdown, 0o644); err != nil {
				return err
			}
			installed = append(installed, dst)
		}
		if *asJSON {
			out, _ := json.MarshalIndent(map[string]any{"installed": installed}, "", "  ")
			fmt.Println(string(out))
			return nil
		}
		for _, p := range installed {
			fmt.Printf("installed %s\n", p)
		}
		return nil
	case "status":
		fs := flag.NewFlagSet("skills status", flag.ContinueOnError)
		customPath := fs.String("path", "", "custom skills base directory")
		asJSON := fs.Bool("json", false, "output JSON")
		if err := fs.Parse(reorderArgs(rest, "path")); err != nil {
			return err
		}
		dirs, err := resolveSkillDirs("all", *customPath)
		if err != nil {
			return err
		}
		type skillStatus struct {
			Path      string `json:"path"`
			Installed bool   `json:"installed"`
		}
		var statuses []skillStatus
		for _, base := range dirs {
			p := filepath.Join(base, "luma-eye", "SKILL.md")
			_, err := os.Stat(p)
			statuses = append(statuses, skillStatus{Path: p, Installed: err == nil})
		}
		if *asJSON {
			out, _ := json.MarshalIndent(map[string]any{"skills": statuses}, "", "  ")
			fmt.Println(string(out))
			return nil
		}
		for _, st := range statuses {
			fmt.Printf("installed=%-5v  %s\n", st.Installed, st.Path)
		}
		return nil
	default:
		return fmt.Errorf("unknown skills subcommand %q (expected install or status)", action)
	}
}

func runStats(args []string) error {
	fs := flag.NewFlagSet("stats", flag.ExitOnError)
	u, t, asJSON := addCommonFlags(fs)
	since := fs.String("since", "24h", "lookback window (e.g. 1h, 24h, 7d)")
	if err := fs.Parse(reorderArgs(args, "url", "token", "since")); err != nil {
		return err
	}
	if fs.NArg() < 1 {
		return fmt.Errorf("usage: luma-eye stats <project>")
	}
	slug := fs.Arg(0)
	cli := newAPIClient(*u, *t)
	var ov clickhouse.OverviewStats
	raw, err := cli.doJSON(http.MethodGet, fmt.Sprintf("/api/v1/query/overview?project=%s&since=%s", url.QueryEscape(slug), url.QueryEscape(*since)), nil, &ov)
	if err != nil {
		return err
	}
	if *asJSON {
		fmt.Println(string(raw))
		return nil
	}
	fmt.Printf("Project: %s (%s)\n", ov.Project, ov.Window)
	fmt.Printf("Active(5m): %d | UV: %d | PV: %d | Custom Events: %d | Sessions: %d | Errors: %d | Crash-Free: %.2f%%\n",
		ov.ActiveUsers5m, ov.UniqueVisitors, ov.Pageviews, ov.CustomEvents, ov.Sessions, ov.ErrorCount, ov.CrashFreeRate)
	if len(ov.VitalsP75) > 0 {
		fmt.Print("Web Vitals P75: ")
		for i, v := range ov.VitalsP75 {
			if i > 0 {
				fmt.Print(" | ")
			}
			fmt.Printf("%s=%.2f (%d)", v.MetricName, v.P75, v.Samples)
		}
		fmt.Println()
	}
	return nil
}

func runFunnel(args []string) error {
	fs := flag.NewFlagSet("funnel", flag.ExitOnError)
	u, t, asJSON := addCommonFlags(fs)
	stepsStr := fs.String("steps", "", "comma-separated funnel steps (event names or page paths)")
	window := fs.Int("window", 3600, "conversion window in seconds")
	since := fs.String("since", "7d", "lookback window")
	if err := fs.Parse(reorderArgs(args, "url", "token", "steps", "window", "since")); err != nil {
		return err
	}
	if fs.NArg() < 1 || *stepsStr == "" {
		return fmt.Errorf("usage: luma-eye funnel <project> --steps \"step1,step2\"")
	}
	slug := fs.Arg(0)
	var steps []string
	for _, s := range strings.Split(*stepsStr, ",") {
		if s = strings.TrimSpace(s); s != "" {
			steps = append(steps, s)
		}
	}
	cli := newAPIClient(*u, *t)
	var res clickhouse.FunnelResult
	raw, err := cli.doJSON(http.MethodPost, "/api/v1/query/funnel", map[string]any{
		"project":        slug,
		"steps":          steps,
		"window_seconds": *window,
		"since":          *since,
	}, &res)
	if err != nil {
		return err
	}
	if *asJSON {
		fmt.Println(string(raw))
		return nil
	}
	fmt.Printf("Funnel for %s (window=%ds, overall=%.2f%%)\n", res.Project, res.WindowSeconds, res.OverallConversion)
	for _, st := range res.Steps {
		fmt.Printf("  %d. %-24s users=%d  overall=%.2f%%  step=%.2f%%\n",
			st.StepIndex, st.Step, st.Users, st.ConversionRate, st.StepRate)
	}
	return nil
}

func runRetention(args []string) error {
	fs := flag.NewFlagSet("retention", flag.ExitOnError)
	u, t, asJSON := addCommonFlags(fs)
	days := fs.Int("days", 7, "retention horizon in days")
	if err := fs.Parse(reorderArgs(args, "url", "token", "days")); err != nil {
		return err
	}
	if fs.NArg() < 1 {
		return fmt.Errorf("usage: luma-eye retention <project>")
	}
	slug := fs.Arg(0)
	cli := newAPIClient(*u, *t)
	var res clickhouse.RetentionResult
	raw, err := cli.doJSON(http.MethodGet, fmt.Sprintf("/api/v1/query/retention?project=%s&days=%d", url.QueryEscape(slug), *days), nil, &res)
	if err != nil {
		return err
	}
	if *asJSON {
		fmt.Println(string(raw))
		return nil
	}
	fmt.Printf("Retention (%d days) for %s:\n", res.Days, res.Project)
	for _, row := range res.Rows {
		fmt.Printf("  %s (n=%d): %v\n", row.CohortDate, row.CohortUsers, row.DayCounts)
	}
	return nil
}

func runIssues(args []string) error {
	fs := flag.NewFlagSet("issues", flag.ExitOnError)
	u, t, asJSON := addCommonFlags(fs)
	since := fs.String("since", "7d", "lookback window")
	status := fs.String("status", "open", "filter status: open|resolved|ignored|all")
	detail := fs.String("detail", "", "show full detail for fingerprint")
	resolve := fs.String("resolve", "", "mark fingerprint resolved")
	ignore := fs.String("ignore", "", "mark fingerprint ignored")
	reopen := fs.String("reopen", "", "reopen fingerprint")
	if err := fs.Parse(reorderArgs(args, "url", "token", "since", "status", "detail", "resolve", "ignore", "reopen")); err != nil {
		return err
	}
	if fs.NArg() < 1 {
		return fmt.Errorf("usage: luma-eye issues <project>")
	}
	slug := fs.Arg(0)
	cli := newAPIClient(*u, *t)

	if *resolve != "" || *ignore != "" || *reopen != "" {
		fp := *resolve
		newSt := "resolved"
		if *ignore != "" {
			fp = *ignore
			newSt = "ignored"
		} else if *reopen != "" {
			fp = *reopen
			newSt = "open"
		}
		raw, err := cli.doJSON(http.MethodPost, fmt.Sprintf("/api/v1/issues/%s/status", url.PathEscape(fp)), map[string]string{
			"project": slug,
			"status":  newSt,
		}, nil)
		if err != nil {
			return err
		}
		fmt.Println(string(raw))
		return nil
	}

	if *detail != "" {
		var d clickhouse.IssueDetail
		raw, err := cli.doJSON(http.MethodGet, fmt.Sprintf("/api/v1/issues/%s?project=%s", url.PathEscape(*detail), url.QueryEscape(slug)), nil, &d)
		if err != nil {
			return err
		}
		if *asJSON {
			fmt.Println(string(raw))
			return nil
		}
		fmt.Printf("[%s] %s: %s (count=%d users=%d release=%s)\nCulprit: %s\nTraceID: %s\n",
			d.Summary.Status, d.Summary.ErrorType, d.Summary.ErrorMessage, d.Summary.Count, d.Summary.Users, d.Release, d.Summary.Culprit, d.TraceID)
		fmt.Println("Frames:")
		for _, f := range d.Frames {
			fmt.Printf("  at %s (%s:%d:%d) [symbolicated=%v]\n", f.Function, f.Filename, f.LineNo, f.ColNo, f.Symbolicated)
			if f.ContextLine != "" {
				fmt.Printf("    > %s\n", f.ContextLine)
			}
		}
		fmt.Printf("Breadcrumbs (%d):\n", len(d.Breadcrumbs))
		for _, bc := range d.Breadcrumbs {
			fmt.Printf("  [%s] %s (%s): %s\n", bc.TS, bc.Category, bc.Level, bc.Message)
		}
		return nil
	}

	var res struct {
		Issues []clickhouse.IssueSummary `json:"issues"`
	}
	raw, err := cli.doJSON(http.MethodGet, fmt.Sprintf("/api/v1/issues?project=%s&since=%s&status=%s", url.QueryEscape(slug), url.QueryEscape(*since), url.QueryEscape(*status)), nil, &res)
	if err != nil {
		return err
	}
	if *asJSON {
		fmt.Println(string(raw))
		return nil
	}
	for _, is := range res.Issues {
		fmt.Printf("[%s] %-16s count=%-5d users=%-4d %s: %s (%s)\n",
			is.Status, is.Fingerprint, is.Count, is.Users, is.ErrorType, is.ErrorMessage, is.Culprit)
	}
	return nil
}

func runLogs(args []string) error {
	fs := flag.NewFlagSet("logs", flag.ExitOnError)
	u, t, asJSON := addCommonFlags(fs)
	level := fs.String("level", "all", "level filter")
	search := fs.String("search", "", "search substring")
	trace := fs.String("trace", "", "filter trace_id")
	since := fs.String("since", "1h", "lookback window")
	limit := fs.Int("limit", 100, "max rows")
	if err := fs.Parse(reorderArgs(args, "url", "token", "level", "search", "trace", "since", "limit")); err != nil {
		return err
	}
	if fs.NArg() < 1 {
		return fmt.Errorf("usage: luma-eye logs <project>")
	}
	slug := fs.Arg(0)
	cli := newAPIClient(*u, *t)
	q := url.Values{}
	q.Set("project", slug)
	q.Set("level", *level)
	q.Set("q", *search)
	q.Set("trace_id", *trace)
	q.Set("since", *since)
	q.Set("limit", strconv.Itoa(*limit))

	var res struct {
		Logs []clickhouse.LogView `json:"logs"`
	}
	raw, err := cli.doJSON(http.MethodGet, "/api/v1/logs?"+q.Encode(), nil, &res)
	if err != nil {
		return err
	}
	if *asJSON {
		fmt.Println(string(raw))
		return nil
	}
	for _, lg := range res.Logs {
		fmt.Printf("%s [%-5s] %s (trace=%s)\n", lg.TS, lg.Level, lg.Message, lg.TraceID)
	}
	return nil
}

func runSourceMaps(args []string) error {
	if len(args) == 0 || args[0] != "upload" {
		return fmt.Errorf("usage: luma-eye sourcemaps upload --project <slug> --release <ver> [--prefix assets/] <dir-or-file>")
	}
	fs := flag.NewFlagSet("sourcemaps", flag.ExitOnError)
	u, t, _ := addCommonFlags(fs)
	proj := fs.String("project", "", "project slug")
	release := fs.String("release", "default", "release version or git sha")
	prefix := fs.String("prefix", "", "optional URL path prefix for asset keys (e.g. assets/)")
	if err := fs.Parse(reorderArgs(args[1:], "url", "token", "project", "release", "prefix")); err != nil {
		return err
	}
	if *proj == "" || fs.NArg() < 1 {
		return fmt.Errorf("usage: luma-eye sourcemaps upload --project <slug> --release <ver> <dir-or-file>")
	}
	targetPath := fs.Arg(0)
	cli := newAPIClient(*u, *t)

	info, err := os.Stat(targetPath)
	if err != nil {
		return err
	}

	var files []string
	if info.IsDir() {
		err = filepath.WalkDir(targetPath, func(p string, d os.DirEntry, wErr error) error {
			if wErr != nil {
				return wErr
			}
			if !d.IsDir() && strings.HasSuffix(d.Name(), ".map") {
				files = append(files, p)
			}
			return nil
		})
		if err != nil {
			return err
		}
	} else {
		files = []string{targetPath}
	}

	if len(files) == 0 {
		return fmt.Errorf("no .map files found in %s", targetPath)
	}

	for _, f := range files {
		rel := filepath.Base(f)
		if info.IsDir() {
			if r, err := filepath.Rel(targetPath, f); err == nil {
				rel = filepath.ToSlash(r)
			}
		}
		assetPath := strings.TrimSuffix(rel, ".map")
		if *prefix != "" {
			assetPath = strings.TrimRight(*prefix, "/") + "/" + strings.TrimLeft(assetPath, "/")
		}
		if err := uploadSingleSourceMap(cli, *proj, *release, assetPath, f); err != nil {
			return err
		}
		fmt.Printf("uploaded %s -> asset=%s (release=%s)\n", f, assetPath, *release)
	}
	return nil
}

func uploadSingleSourceMap(cli *apiClient, projectSlug, release, assetPath, filePath string) error {
	f, err := os.Open(filePath)
	if err != nil {
		return err
	}
	defer f.Close()

	var body bytes.Buffer
	mw := multipart.NewWriter(&body)
	_ = mw.WriteField("project", projectSlug)
	_ = mw.WriteField("release", release)
	_ = mw.WriteField("asset", assetPath)
	fw, err := mw.CreateFormFile("file", filepath.Base(filePath))
	if err != nil {
		return err
	}
	if _, err := io.Copy(fw, f); err != nil {
		return err
	}
	if err := mw.Close(); err != nil {
		return err
	}

	req, err := http.NewRequest(http.MethodPost, cli.baseURL+"/api/v1/sourcemaps", &body)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", mw.FormDataContentType())
	if cli.token != "" {
		req.Header.Set("Authorization", "Bearer "+cli.token)
	}
	resp, err := cli.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if resp.StatusCode >= 300 {
		return fmt.Errorf("upload %s failed (HTTP %d): %s", filePath, resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	return nil
}

func runGrafana(args []string) error {
	if len(args) == 0 || args[0] != "render" {
		return fmt.Errorf("usage: luma-eye grafana render --project <slug> [--datasource victoria|clickhouse] [--output <path>]")
	}
	fs := flag.NewFlagSet("grafana", flag.ExitOnError)
	proj := fs.String("project", "", "project slug")
	ds := fs.String("datasource", "victoria", "datasource mode: victoria or clickhouse")
	out := fs.String("output", "", "output file path (default stdout)")
	if err := fs.Parse(reorderArgs(args[1:], "project", "datasource", "output")); err != nil {
		return err
	}
	if *proj == "" {
		return fmt.Errorf("--project is required")
	}
	data, err := observe.RenderGrafanaDashboard(*proj, *ds)
	if err != nil {
		return err
	}
	if *out == "" {
		fmt.Println(string(data))
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(*out), 0o755); err != nil {
		return err
	}
	return os.WriteFile(*out, append(data, '\n'), 0o644)
}

func runHealthcheck(args []string) error {
	fs := flag.NewFlagSet("healthcheck", flag.ExitOnError)
	target := fs.String("url", "http://127.0.0.1:8090/healthz", "healthcheck URL")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cli := &http.Client{Timeout: 3 * time.Second}
	resp, err := cli.Get(*target)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("healthcheck status %d", resp.StatusCode)
	}
	return nil
}
