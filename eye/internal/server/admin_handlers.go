package server

import (
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/liutianjie/luma-eye/internal/clickhouse"
	"github.com/liutianjie/luma-eye/internal/model"
	"github.com/liutianjie/luma-eye/internal/observe"
	"github.com/liutianjie/luma-eye/internal/project"
)

func (s *Server) handleListProjects(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"projects":   s.store.List(),
		"public_url": s.resolvePublicURL(r),
	})
}

func (s *Server) handleEnsureProject(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Slug           string   `json:"slug"`
		Name           string   `json:"name"`
		AllowedOrigins []string `json:"allowed_origins"`
		RateLimitRPS   int      `json:"rate_limit_rps"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 64<<10)).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return
	}
	proj, created, err := s.store.EnsureProject(body.Slug, body.Name, body.AllowedOrigins, body.RateLimitRPS)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	base := s.resolvePublicURL(r)
	status := http.StatusOK
	if created {
		status = http.StatusCreated
	}
	writeJSON(w, status, map[string]any{
		"created":    created,
		"project":    proj,
		"sentry_dsn": buildSentryDSN(base, proj),
		"ingest_url": base + "/_e/b",
	})
}

func (s *Server) handleDeleteProject(w http.ResponseWriter, r *http.Request) {
	slug := r.PathValue("slug")
	if err := s.store.DeleteProject(slug); err != nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"deleted": slug})
}

func (s *Server) handleQueryPortfolio(w http.ResponseWriter, r *http.Request) {
	since := clickhouse.ParseSince(r.URL.Query().Get("since"), 24*time.Hour)
	projects := s.store.List()
	statesByProject := make(map[string]map[string]model.IssueState, len(projects))
	for _, p := range projects {
		statesByProject[p.Slug] = s.store.GetIssueStates(p.Slug)
	}
	res, err := s.ch.GetPortfolioOverview(r.Context(), projects, statesByProject, since)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handleQueryOverview(w http.ResponseWriter, r *http.Request) {
	projSlug, ok := s.resolveProjectParam(w, r.URL.Query().Get("project"))
	if !ok {
		return
	}
	since := clickhouse.ParseSince(r.URL.Query().Get("since"), 24*time.Hour)
	stats, err := s.ch.GetOverview(r.Context(), projSlug, since)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, stats)
}

func (s *Server) handleQueryPages(w http.ResponseWriter, r *http.Request) {
	projSlug, ok := s.resolveProjectParam(w, r.URL.Query().Get("project"))
	if !ok {
		return
	}
	q := r.URL.Query()
	since := clickhouse.ParseSince(q.Get("since"), 24*time.Hour)
	limit, _ := strconv.Atoi(q.Get("limit"))
	pages, err := s.ch.GetPageMetrics(r.Context(), projSlug, since, limit)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"pages": pages})
}

func (s *Server) handleListEvents(w http.ResponseWriter, r *http.Request) {
	projSlug, ok := s.resolveProjectParam(w, r.URL.Query().Get("project"))
	if !ok {
		return
	}
	q := r.URL.Query()
	limit, _ := strconv.Atoi(q.Get("limit"))
	events, err := s.ch.ListEvents(r.Context(), projSlug, clickhouse.EventQueryFilter{
		Since:     clickhouse.ParseSince(q.Get("since"), 24*time.Hour),
		EventType: q.Get("event_type"),
		EventName: q.Get("event_name"),
		ActorID:   q.Get("actor_id"),
		SessionID: q.Get("session_id"),
		Country:   q.Get("country"),
		Search:    q.Get("q"),
		Limit:     limit,
	})
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": events})
}

func (s *Server) handleListSessions(w http.ResponseWriter, r *http.Request) {
	projSlug, ok := s.resolveProjectParam(w, r.URL.Query().Get("project"))
	if !ok {
		return
	}
	q := r.URL.Query()
	limit, _ := strconv.Atoi(q.Get("limit"))
	hasErr := strings.EqualFold(q.Get("has_error"), "true") || q.Get("has_error") == "1"
	sessions, err := s.ch.ListSessions(r.Context(), projSlug, clickhouse.SessionQueryFilter{
		Since:    clickhouse.ParseSince(q.Get("since"), 7*24*time.Hour),
		ActorID:  q.Get("actor_id"),
		Country:  q.Get("country"),
		Search:   q.Get("q"),
		HasError: hasErr,
		Limit:    limit,
	})
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"sessions": sessions})
}

func (s *Server) handleGetTimeline(w http.ResponseWriter, r *http.Request) {
	projSlug, ok := s.resolveProjectParam(w, r.URL.Query().Get("project"))
	if !ok {
		return
	}
	q := r.URL.Query()
	sessionID := strings.TrimSpace(q.Get("session_id"))
	actorID := strings.TrimSpace(q.Get("actor_id"))
	if sessionID == "" && actorID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "session_id or actor_id is required"})
		return
	}
	limit, _ := strconv.Atoi(q.Get("limit"))
	tl, err := s.ch.GetTimeline(
		r.Context(),
		projSlug,
		sessionID,
		actorID,
		clickhouse.ParseSince(q.Get("since"), 7*24*time.Hour),
		limit,
	)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, tl)
}

func (s *Server) handleQueryFunnel(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Project       string   `json:"project"`
		Steps         []string `json:"steps"`
		WindowSeconds int      `json:"window_seconds"`
		Since         string   `json:"since"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 32<<10)).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return
	}
	if !project.ValidateSlug(body.Project) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid project slug"})
		return
	}
	since := clickhouse.ParseSince(body.Since, 7*24*time.Hour)
	res, err := s.ch.GetFunnel(r.Context(), body.Project, body.Steps, body.WindowSeconds, since)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handleQueryRetention(w http.ResponseWriter, r *http.Request) {
	projSlug, ok := s.resolveProjectParam(w, r.URL.Query().Get("project"))
	if !ok {
		return
	}
	days, _ := strconv.Atoi(r.URL.Query().Get("days"))
	res, err := s.ch.GetRetention(r.Context(), projSlug, days)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handleListIssues(w http.ResponseWriter, r *http.Request) {
	projSlug, ok := s.resolveProjectParam(w, r.URL.Query().Get("project"))
	if !ok {
		return
	}
	since := clickhouse.ParseSince(r.URL.Query().Get("since"), 7*24*time.Hour)
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	statusFilter := strings.TrimSpace(r.URL.Query().Get("status"))
	states := s.store.GetIssueStates(projSlug)

	issues, err := s.ch.ListIssues(r.Context(), projSlug, since, limit, states, statusFilter)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"issues": issues})
}

func (s *Server) handleIssueDetail(w http.ResponseWriter, r *http.Request) {
	projSlug, ok := s.resolveProjectParam(w, r.URL.Query().Get("project"))
	if !ok {
		return
	}
	fp := strings.TrimSpace(r.PathValue("fingerprint"))
	if fp == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "missing fingerprint"})
		return
	}
	states := s.store.GetIssueStates(projSlug)
	detail, err := s.ch.GetIssueDetail(r.Context(), projSlug, fp, states)
	if err != nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, detail)
}

func (s *Server) handleSetIssueStatus(w http.ResponseWriter, r *http.Request) {
	fp := strings.TrimSpace(r.PathValue("fingerprint"))
	var body struct {
		Project string            `json:"project"`
		Status  model.IssueStatus `json:"status"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return
	}
	if !project.ValidateSlug(body.Project) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid project slug"})
		return
	}
	st, err := s.store.SetIssueStatus(body.Project, fp, body.Status)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, st)
}

func (s *Server) handleSearchLogs(w http.ResponseWriter, r *http.Request) {
	projSlug, ok := s.resolveProjectParam(w, r.URL.Query().Get("project"))
	if !ok {
		return
	}
	q := r.URL.Query()
	since := clickhouse.ParseSince(q.Get("since"), time.Hour)
	limit, _ := strconv.Atoi(q.Get("limit"))
	logs, err := s.ch.SearchLogsFiltered(r.Context(), projSlug, q.Get("level"), q.Get("q"), q.Get("trace_id"), q.Get("actor_id"), q.Get("session_id"), since, limit)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"logs": logs})
}

func (s *Server) handleUploadSourceMap(w http.ResponseWriter, r *http.Request) {
	ct := strings.ToLower(r.Header.Get("Content-Type"))
	var projSlug, release, asset string
	var mapBytes []byte

	if strings.HasPrefix(ct, "multipart/form-data") {
		if err := r.ParseMultipartForm(32 << 20); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid multipart form"})
			return
		}
		projSlug = strings.TrimSpace(r.FormValue("project"))
		release = strings.TrimSpace(r.FormValue("release"))
		asset = strings.TrimSpace(r.FormValue("asset"))
		f, hdr, err := r.FormFile("file")
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "missing sourcemap file"})
			return
		}
		defer f.Close()
		if asset == "" && hdr != nil {
			asset = strings.TrimSuffix(hdr.Filename, ".map")
		}
		mapBytes, err = io.ReadAll(io.LimitReader(f, 32<<20))
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "read file failed"})
			return
		}
	} else {
		var body struct {
			Project string          `json:"project"`
			Release string          `json:"release"`
			Asset   string          `json:"asset"`
			Map     json.RawMessage `json:"map"`
		}
		if err := json.NewDecoder(io.LimitReader(r.Body, 32<<20)).Decode(&body); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
			return
		}
		projSlug = strings.TrimSpace(body.Project)
		release = strings.TrimSpace(body.Release)
		asset = strings.TrimSpace(body.Asset)
		if len(body.Map) > 0 && body.Map[0] == '"' {
			var sMap string
			_ = json.Unmarshal(body.Map, &sMap)
			mapBytes = []byte(sMap)
		} else {
			mapBytes = body.Map
		}
	}

	if !project.ValidateSlug(projSlug) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid project slug"})
		return
	}
	norm, err := s.symbolicator.Upload(projSlug, release, asset, mapBytes)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":         true,
		"project":    projSlug,
		"release":    defaultStr(release, "default"),
		"asset_key":  norm,
		"size_bytes": len(mapBytes),
	})
}

func (s *Server) handleGrafanaDashboard(w http.ResponseWriter, r *http.Request) {
	projSlug, ok := s.resolveProjectParam(w, r.URL.Query().Get("project"))
	if !ok {
		return
	}
	ds := r.URL.Query().Get("datasource")
	raw, err := observe.RenderGrafanaDashboard(projSlug, ds)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(raw)
}

func (s *Server) resolveProjectParam(w http.ResponseWriter, slug string) (string, bool) {
	slug = strings.TrimSpace(slug)
	if !project.ValidateSlug(slug) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "valid ?project=<slug> is required"})
		return "", false
	}
	return slug, true
}

func (s *Server) resolvePublicURL(r *http.Request) string {
	if s.cfg.PublicURL != "" {
		return s.cfg.PublicURL
	}
	scheme := "http"
	if r.TLS != nil || strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https") {
		scheme = "https"
	}
	if r.Host != "" {
		return scheme + "://" + r.Host
	}
	return "http://127.0.0.1" + s.cfg.ListenAddr
}

func buildSentryDSN(baseURL string, proj *model.Project) string {
	scheme, rest, ok := strings.Cut(baseURL, "://")
	if !ok {
		return ""
	}
	return scheme + "://" + proj.PublicKey + "@" + rest + "/" + strconv.Itoa(proj.DSNID)
}
