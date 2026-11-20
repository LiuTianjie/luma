package project

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/liutianjie/luma-eye/internal/model"
)

var (
	slugRe            = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)
	ErrNotFound       = errors.New("project not found")
	ErrInvalidSlug    = errors.New("invalid project slug: must match ^[a-z0-9][a-z0-9_-]{0,62}$")
	ErrUnauthorized   = errors.New("invalid public key")
	ErrOriginRejected = errors.New("origin not allowed for project")
)

type Store struct {
	mu       sync.RWMutex
	ioMu     sync.Mutex
	dataDir  string
	metaDir  string
	readOnly bool

	projects       map[string]*model.Project // slug -> project
	byDSNID        map[int]*model.Project
	byPublicKey    map[string]*model.Project
	nextDSNID      int
	issues         map[string]model.IssueState // "project\x00fingerprint" -> state
	issuesDirty    bool
	lastIssueFlush time.Time
}

type projectsFile struct {
	NextDSNID int              `json:"next_dsn_id"`
	Projects  []*model.Project `json:"projects"`
}

type issuesFile struct {
	Issues []model.IssueState `json:"issues"`
}

func NewStore(dataDir string, readOnly bool) (*Store, error) {
	metaDir := filepath.Join(dataDir, "meta")
	if !readOnly && dataDir != "" {
		if err := os.MkdirAll(metaDir, 0o755); err != nil {
			return nil, fmt.Errorf("create meta dir: %w", err)
		}
	}
	s := &Store{
		dataDir:     dataDir,
		metaDir:     metaDir,
		readOnly:    readOnly,
		projects:    make(map[string]*model.Project),
		byDSNID:     make(map[int]*model.Project),
		byPublicKey: make(map[string]*model.Project),
		nextDSNID:   1,
		issues:      make(map[string]model.IssueState),
	}
	if dataDir != "" {
		if err := s.load(); err != nil {
			return nil, err
		}
	}
	return s, nil
}

func (s *Store) load() error {
	pPath := filepath.Join(s.metaDir, "projects.json")
	if raw, err := os.ReadFile(pPath); err == nil {
		var pf projectsFile
		if err := json.Unmarshal(raw, &pf); err != nil {
			return fmt.Errorf("parse %s: %w", pPath, err)
		}
		if pf.NextDSNID > s.nextDSNID {
			s.nextDSNID = pf.NextDSNID
		}
		for _, p := range pf.Projects {
			if p == nil || p.Slug == "" {
				continue
			}
			if p.RateLimitRPS <= 0 {
				p.RateLimitRPS = 200
			}
			s.indexProjectLocked(p)
			if p.DSNID >= s.nextDSNID {
				s.nextDSNID = p.DSNID + 1
			}
		}
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("read %s: %w", pPath, err)
	}

	iPath := filepath.Join(s.metaDir, "issues_state.json")
	if raw, err := os.ReadFile(iPath); err == nil {
		var inf issuesFile
		if err := json.Unmarshal(raw, &inf); err != nil {
			return fmt.Errorf("parse %s: %w", iPath, err)
		}
		for _, st := range inf.Issues {
			if st.Project == "" || st.Fingerprint == "" {
				continue
			}
			s.issues[issueKey(st.Project, st.Fingerprint)] = st
		}
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("read %s: %w", iPath, err)
	}
	return nil
}

func (s *Store) indexProjectLocked(p *model.Project) {
	s.projects[p.Slug] = p
	s.byDSNID[p.DSNID] = p
	s.byPublicKey[p.PublicKey] = p
}

func ValidateSlug(slug string) bool {
	return slugRe.MatchString(slug)
}

func (s *Store) List() []*model.Project {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*model.Project, 0, len(s.projects))
	for _, p := range s.projects {
		cp := cloneProject(p)
		out = append(out, cp)
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].DSNID < out[j].DSNID
	})
	return out
}

func (s *Store) Get(slugOrID string) (*model.Project, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if p, ok := s.projects[slugOrID]; ok {
		return cloneProject(p), true
	}
	if id, err := strconv.Atoi(slugOrID); err == nil {
		if p, ok := s.byDSNID[id]; ok {
			return cloneProject(p), true
		}
	}
	return nil, false
}

func (s *Store) EnsureProject(slug, name string, allowedOrigins []string, rateLimitRPS int) (*model.Project, bool, error) {
	slug = strings.ToLower(strings.TrimSpace(slug))
	if !ValidateSlug(slug) {
		return nil, false, ErrInvalidSlug
	}
	if name == "" {
		name = slug
	}
	if rateLimitRPS <= 0 {
		rateLimitRPS = 200
	}
	cleanOrigins := normalizeOrigins(allowedOrigins)

	s.mu.Lock()
	defer s.mu.Unlock()
	if existing, ok := s.projects[slug]; ok {
		changed := false
		if name != "" && name != existing.Name && existing.Name == existing.Slug {
			existing.Name = name
			changed = true
		}
		if len(cleanOrigins) > 0 && !stringSlicesEqual(existing.AllowedOrigins, cleanOrigins) {
			existing.AllowedOrigins = cleanOrigins
			changed = true
		}
		if rateLimitRPS != existing.RateLimitRPS && rateLimitRPS > 0 {
			existing.RateLimitRPS = rateLimitRPS
			changed = true
		}
		if changed && !s.readOnly && s.dataDir != "" {
			if err := s.saveProjectsLocked(); err != nil {
				return nil, false, err
			}
		}
		return cloneProject(existing), false, nil
	}

	p := &model.Project{
		DSNID:          s.nextDSNID,
		Slug:           slug,
		Name:           name,
		PublicKey:      generatePublicKey(slug),
		AllowedOrigins: cleanOrigins,
		RateLimitRPS:   rateLimitRPS,
		CreatedAt:      time.Now().UTC(),
	}
	s.nextDSNID++
	s.indexProjectLocked(p)
	if !s.readOnly && s.dataDir != "" {
		if err := s.saveProjectsLocked(); err != nil {
			return nil, false, err
		}
	}
	return cloneProject(p), true, nil
}

func (s *Store) DeleteProject(slug string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.projects[slug]
	if !ok {
		return ErrNotFound
	}
	delete(s.projects, p.Slug)
	delete(s.byDSNID, p.DSNID)
	delete(s.byPublicKey, p.PublicKey)
	if !s.readOnly && s.dataDir != "" {
		return s.saveProjectsLocked()
	}
	return nil
}

func (s *Store) ReplaceAllProjects(projects []*model.Project) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.projects = make(map[string]*model.Project, len(projects))
	s.byDSNID = make(map[int]*model.Project, len(projects))
	s.byPublicKey = make(map[string]*model.Project, len(projects))
	maxID := 0
	for _, p := range projects {
		if p == nil || p.Slug == "" {
			continue
		}
		cp := cloneProject(p)
		if cp.RateLimitRPS <= 0 {
			cp.RateLimitRPS = 200
		}
		s.indexProjectLocked(cp)
		if cp.DSNID > maxID {
			maxID = cp.DSNID
		}
	}
	if maxID >= s.nextDSNID {
		s.nextDSNID = maxID + 1
	}
	if !s.readOnly && s.dataDir != "" {
		return s.saveProjectsLocked()
	}
	return nil
}

// ResolveForIngest resolves a project by key/slug/dsnID, checks Origin allowlist, and
// optionally auto-provisions a project when AutoProvisionKey matches and Origin matches allowed suffixes.
func (s *Store) ResolveForIngest(projectHint, publicKey, originHeader, autoKey string, autoSuffixes []string) (*model.Project, error) {
	projectHint = strings.TrimSpace(projectHint)
	publicKey = strings.TrimSpace(publicKey)
	originHeader = strings.TrimSpace(originHeader)

	s.mu.RLock()
	var matched *model.Project
	if publicKey != "" {
		if p, ok := s.byPublicKey[publicKey]; ok {
			matched = p
		}
	}
	if matched != nil && projectHint != "" {
		if matched.Slug != projectHint && strconv.Itoa(matched.DSNID) != projectHint {
			// If caller explicitly passed a different project slug/id along with a mismatched key, reject unless autoKey matches.
			matched = nil
		}
	}
	if matched == nil && projectHint != "" && autoKey != "" && subtle.ConstantTimeCompare([]byte(publicKey), []byte(autoKey)) == 1 {
		if p, ok := s.projects[strings.ToLower(projectHint)]; ok {
			matched = p
		} else if id, err := strconv.Atoi(projectHint); err == nil {
			if p, ok := s.byDSNID[id]; ok {
				matched = p
			}
		}
	}
	var cp *model.Project
	if matched != nil {
		cp = cloneProject(matched)
	}
	s.mu.RUnlock()

	if cp != nil {
		if originHeader != "" && len(cp.AllowedOrigins) > 0 && !OriginMatches(originHeader, cp.AllowedOrigins) {
			return nil, ErrOriginRejected
		}
		return cp, nil
	}

	// Auto-provision path for zero-friction Vibe Coding onboarding
	if projectHint != "" && autoKey != "" && subtle.ConstantTimeCompare([]byte(publicKey), []byte(autoKey)) == 1 {
		slug := strings.ToLower(projectHint)
		if !ValidateSlug(slug) {
			return nil, ErrInvalidSlug
		}
		var origins []string
		if originHeader != "" {
			if !OriginMatchesSuffix(originHeader, autoSuffixes) {
				return nil, ErrOriginRejected
			}
			origins = []string{originHeader}
		}
		created, _, err := s.EnsureProject(slug, slug, origins, 200)
		if err != nil {
			return nil, err
		}
		return created, nil
	}

	if publicKey == "" {
		return nil, ErrUnauthorized
	}
	return nil, ErrUnauthorized
}

func (s *Store) CheckAndRecordIssue(project, fingerprint string, now time.Time) (bool, string, model.IssueState) {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	key := issueKey(project, fingerprint)

	var (
		shouldAlert bool
		reason      string
		st          model.IssueState
	)
	s.mu.Lock()
	existing, exists := s.issues[key]
	if !exists {
		st = model.IssueState{
			Project:     project,
			Fingerprint: fingerprint,
			Status:      model.IssueOpen,
			FirstSeen:   now,
			LastAlertAt: now,
			UpdatedAt:   now,
		}
		s.issues[key] = st
		shouldAlert = true
		reason = "new_issue"
	} else if existing.Status == model.IssueResolved {
		st = existing
		st.Status = model.IssueOpen
		st.LastAlertAt = now
		st.UpdatedAt = now
		s.issues[key] = st
		shouldAlert = true
		reason = "regression"
	} else {
		st = existing
	}
	s.mu.Unlock()

	if shouldAlert {
		s.saveIssuesBestEffort()
	}
	return shouldAlert, reason, st
}

func (s *Store) SetIssueStatus(project, fingerprint string, status model.IssueStatus) (model.IssueState, error) {
	switch status {
	case model.IssueOpen, model.IssueResolved, model.IssueIgnored:
	default:
		return model.IssueState{}, fmt.Errorf("invalid issue status %q", status)
	}
	now := time.Now().UTC()
	key := issueKey(project, fingerprint)

	s.mu.Lock()
	st, exists := s.issues[key]
	if !exists {
		st = model.IssueState{
			Project:     project,
			Fingerprint: fingerprint,
			FirstSeen:   now,
		}
	}
	st.Status = status
	st.UpdatedAt = now
	s.issues[key] = st
	s.mu.Unlock()

	if err := s.saveIssues(); err != nil {
		return st, err
	}
	return st, nil
}

func (s *Store) GetIssueStates(project string) map[string]model.IssueState {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make(map[string]model.IssueState)
	prefix := project + "\x00"
	for k, v := range s.issues {
		if strings.HasPrefix(k, prefix) {
			out[v.Fingerprint] = v
		}
	}
	return out
}

// PruneStaleIssues removes issue states older than maxAge (keeping ignored issues
// up to 4x maxAge so intentionally silenced noise is not prematurely un-ignored).
func (s *Store) PruneStaleIssues(maxAge time.Duration) int {
	if s == nil || maxAge <= 0 {
		return 0
	}
	now := time.Now().UTC()
	s.mu.Lock()
	removed := 0
	for k, st := range s.issues {
		if st.UpdatedAt.IsZero() {
			continue
		}
		limit := maxAge
		if st.Status == model.IssueIgnored {
			limit = maxAge * 4
		}
		if now.Sub(st.UpdatedAt) > limit {
			delete(s.issues, k)
			removed++
		}
	}
	s.mu.Unlock()
	if removed > 0 {
		s.saveIssuesBestEffort()
	}
	return removed
}

func (s *Store) saveProjectsLocked() error {
	list := make([]*model.Project, 0, len(s.projects))
	for _, p := range s.projects {
		list = append(list, p)
	}
	sort.Slice(list, func(i, j int) bool {
		return list[i].DSNID < list[j].DSNID
	})
	payload := projectsFile{
		NextDSNID: s.nextDSNID,
		Projects:  list,
	}
	return writeAtomicJSON(filepath.Join(s.metaDir, "projects.json"), payload)
}

// saveIssues snapshots issues under a brief RLock and performs JSON encoding + fsync
// serialized by ioMu so ResolveForIngest readers are never blocked by disk I/O.
func (s *Store) saveIssues() error {
	if s.readOnly || s.dataDir == "" {
		return nil
	}
	s.ioMu.Lock()
	defer s.ioMu.Unlock()

	s.mu.RLock()
	list := make([]model.IssueState, 0, len(s.issues))
	for _, st := range s.issues {
		list = append(list, st)
	}
	s.mu.RUnlock()

	sort.Slice(list, func(i, j int) bool {
		if list[i].Project != list[j].Project {
			return list[i].Project < list[j].Project
		}
		return list[i].Fingerprint < list[j].Fingerprint
	})
	return writeAtomicJSON(filepath.Join(s.metaDir, "issues_state.json"), issuesFile{Issues: list})
}

func (s *Store) saveIssuesBestEffort() {
	_ = s.saveIssues()
}

func writeAtomicJSON(path string, v any) error {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), filepath.Base(path)+".*.tmp")
	if err != nil {
		return err
	}
	tmp := f.Name()
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return err
	}
	if err := f.Chmod(0o644); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return err
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, path)
}

func issueKey(project, fingerprint string) string {
	return project + "\x00" + fingerprint
}

func cloneProject(p *model.Project) *model.Project {
	cp := *p
	if p.AllowedOrigins != nil {
		cp.AllowedOrigins = append([]string(nil), p.AllowedOrigins...)
	}
	return &cp
}

func generatePublicKey(slug string) string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	prefix := slug
	if len(prefix) > 16 {
		prefix = prefix[:16]
	}
	return fmt.Sprintf("pk_%s_%s", prefix, hex.EncodeToString(b))
}

func normalizeOrigins(origins []string) []string {
	if len(origins) == 0 {
		return nil
	}
	seen := make(map[string]struct{}, len(origins))
	out := make([]string, 0, len(origins))
	for _, o := range origins {
		o = strings.TrimRight(strings.TrimSpace(o), "/")
		if o == "" {
			continue
		}
		if _, ok := seen[o]; !ok {
			seen[o] = struct{}{}
			out = append(out, o)
		}
	}
	return out
}

func stringSlicesEqual(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func OriginMatches(origin string, allowed []string) bool {
	if len(allowed) == 0 {
		return true
	}
	origin = strings.TrimRight(strings.ToLower(strings.TrimSpace(origin)), "/")
	if origin == "" {
		return true
	}
	u, err := url.Parse(origin)
	host := ""
	if err == nil {
		host = strings.ToLower(u.Hostname())
	}

	for _, rule := range allowed {
		rule = strings.TrimRight(strings.ToLower(strings.TrimSpace(rule)), "/")
		if rule == "*" || rule == origin {
			return true
		}
		if strings.HasPrefix(rule, "*.") {
			suffix := rule[1:] // ".example.com"
			if host != "" && (strings.HasSuffix(host, suffix) || host == strings.TrimPrefix(suffix, ".")) {
				return true
			}
		}
		if strings.Contains(rule, "://*.") {
			parts := strings.SplitN(rule, "://*.", 2)
			if len(parts) == 2 && u != nil && strings.EqualFold(u.Scheme, parts[0]) {
				domain := parts[1]
				if host == domain || strings.HasSuffix(host, "."+domain) {
					return true
				}
			}
		}
	}
	return false
}

func OriginMatchesSuffix(origin string, suffixes []string) bool {
	if len(suffixes) == 0 {
		return false
	}
	u, err := url.Parse(strings.TrimSpace(origin))
	if err != nil {
		return false
	}
	host := strings.ToLower(u.Hostname())
	if host == "" {
		return false
	}
	for _, s := range suffixes {
		s = strings.ToLower(strings.TrimSpace(s))
		if s == "" {
			continue
		}
		if s == "*" {
			return true
		}
		if strings.HasPrefix(s, ".") {
			if strings.HasSuffix(host, s) || host == strings.TrimPrefix(s, ".") {
				return true
			}
		} else if host == s || strings.HasSuffix(host, "."+s) {
			return true
		}
	}
	return false
}
