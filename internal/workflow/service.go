package workflow

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode/utf8"
)

var (
	ErrInvalid  = errors.New("invalid workflow")
	ErrNotFound = errors.New("workflow not found")
	ErrConflict = errors.New("deployment workflow changed while this deployment was running; the newer record was preserved")
)

func invalid(message string) error { return fmt.Errorf("%w: %s", ErrInvalid, message) }

type Selector struct {
	Name    string `json:"name"`
	RepoURL string `json:"repoUrl"`
}
type ResolvedSelector struct {
	Name    string `json:"name"`
	RepoKey string `json:"repoKey"`
}
type Record struct {
	Name        string          `json:"name"`
	Slug        string          `json:"slug"`
	Recipe      Recipe          `json:"recipe"`
	Source      string          `json:"source"`
	RepoKey     string          `json:"repoKey"`
	Note        string          `json:"note"`
	UpdatedAt   int64           `json:"updatedAt"`
	Revision    int64           `json:"revision"`
	LastSuccess json.RawMessage `json:"lastSuccess,omitempty"`
}
type Catalog struct {
	Records       map[string]Record
	ProviderBases map[string]string
}
type Repository interface {
	Workflows(context.Context) (Catalog, error)
	// UpdateWorkflow reauthenticates the management principal and computes the
	// new record inside the same write transaction as its revision check.
	UpdateWorkflow(context.Context, string, func(Catalog) (Record, error)) (Record, error)
}
type CheckRequest struct {
	Recipe   Recipe   `json:"recipe"`
	Selector Selector `json:"selector"`
}
type RecordRequest struct {
	CheckRequest
	Name             string                     `json:"name"`
	Source           string                     `json:"source"`
	Note             *string                    `json:"note"`
	ExpectedRevision *int64                     `json:"expectedRevision"`
	Evidence         map[string]json.RawMessage `json:"evidence"`
}
type Difference struct {
	Field     string `json:"field"`
	Previous  any    `json:"previous"`
	Requested any    `json:"requested"`
}
type CheckReply struct {
	Status      string           `json:"status"`
	Workflow    *Record          `json:"workflow,omitempty"`
	Differences []Difference     `json:"differences"`
	Selector    ResolvedSelector `json:"selector"`
}
type Service struct {
	Repository Repository
	Now        func() time.Time
}

var slugPattern = regexp.MustCompile(`[^a-zA-Z0-9-]+`)

func Slug(name string) string {
	return strings.Trim(slugPattern.ReplaceAllString(strings.ToLower(strings.TrimSpace(name)), "-"), "-")
}
func name(value string) (string, error) {
	if strings.TrimSpace(value) == "" || utf8.RuneCountInString(value) > 200 || Slug(value) == "" {
		return "", invalid("workflow application name is required (maximum 200 characters, with a nonempty slug)")
	}
	return strings.TrimSpace(value), nil
}
func recipeRepo(c Catalog, fields map[string]any) (string, error) {
	text := func(key string) string { s, _ := fields[key].(string); return s }
	u := text("repo")
	if u == "" {
		u = text("repo_url")
	}
	if u != "" {
		return RepoKey(u)
	}
	provider, repository := text("provider_id"), text("repository")
	if provider == "" || repository == "" {
		return "", nil
	}
	base := c.ProviderBases[provider]
	if base == "" && strings.HasPrefix(provider, "github:") {
		base = "https://github.com"
	}
	if base != "" {
		return RepoKey(strings.TrimRight(base, "/") + "/" + repository)
	}
	return provider + ":" + strings.ToLower(repository), nil
}
func selector(c Catalog, raw Selector, fields map[string]any) (ResolvedSelector, error) {
	s := ResolvedSelector{}
	var err error
	if raw.Name != "" {
		s.Name, err = name(raw.Name)
		if err != nil {
			return s, err
		}
	}
	if utf8.RuneCountInString(raw.RepoURL) > 4096 {
		return s, invalid("invalid workflow repository")
	}
	s.RepoKey, err = recipeRepo(c, fields)
	if err == nil && s.RepoKey == "" {
		s.RepoKey, err = RepoKey(raw.RepoURL)
	}
	return s, err
}
func (s Service) List(ctx context.Context) ([]Record, error) {
	c, err := s.Repository.Workflows(ctx)
	if err != nil {
		return nil, err
	}
	rows := make([]Record, 0, len(c.Records))
	for _, record := range c.Records {
		rows = append(rows, record)
	}
	slices.SortFunc(rows, func(a, b Record) int { return strings.Compare(a.Name, b.Name) })
	return rows, nil
}
func (s Service) Get(ctx context.Context, value string) (Record, error) {
	n, err := name(value)
	if err != nil {
		return Record{}, err
	}
	c, err := s.Repository.Workflows(ctx)
	if err != nil {
		return Record{}, err
	}
	r, ok := c.Records[Slug(n)]
	if !ok {
		return Record{}, ErrNotFound
	}
	return r, nil
}
func (s Service) Check(ctx context.Context, request CheckRequest) (CheckReply, error) {
	_, after, err := ValidateRecipe(request.Recipe)
	if err != nil {
		return CheckReply{}, err
	}
	c, err := s.Repository.Workflows(ctx)
	if err != nil {
		return CheckReply{}, err
	}
	sel, err := selector(c, request.Selector, after)
	if err != nil {
		return CheckReply{}, err
	}
	reply := CheckReply{Status: "unrecorded", Selector: sel, Differences: []Difference{}}
	rows := []Record{}
	if sel.Name != "" {
		if r, ok := c.Records[Slug(sel.Name)]; ok {
			rows = append(rows, r)
		}
	} else if sel.RepoKey != "" {
		for _, r := range c.Records {
			if r.RepoKey == sel.RepoKey {
				rows = append(rows, r)
			}
		}
	}
	if len(rows) > 1 {
		matches := []Record{}
		sidecar := after["compose_sidecar"]
		if sidecar == nil || sidecar == "" {
			sidecar = after["sidecar"]
		}
		for _, r := range rows {
			_, fields, err := ValidateRecipe(r.Recipe)
			if err != nil {
				return reply, err
			}
			other := fields["compose_sidecar"]
			if other == nil || other == "" {
				other = fields["sidecar"]
			}
			if sidecar != nil && sidecar != "" && sidecar == other {
				matches = append(matches, r)
			}
		}
		if len(matches) != 1 {
			return reply, invalid("multiple application workflows match this repository; select one with --workflow-app")
		}
		rows = matches
	}
	if len(rows) == 0 {
		return reply, nil
	}
	previous := rows[0]
	_, before, err := ValidateRecipe(previous.Recipe)
	if err != nil {
		return reply, err
	}
	for _, item := range []struct {
		fields   map[string]any
		fallback string
	}{{before, previous.RepoKey}, {after, sel.RepoKey}} {
		repo, err := recipeRepo(c, item.fields)
		if err != nil {
			return reply, err
		}
		if repo == "" {
			repo = item.fallback
		}
		for _, key := range []string{"provider_id", "repository", "repo", "repo_url"} {
			delete(item.fields, key)
		}
		item.fields["repository"] = repo
	}
	keys := slices.AppendSeq(slices.Collect(maps.Keys(before)), maps.Keys(after))
	slices.Sort(keys)
	for _, key := range slices.Compact(keys) {
		// JSON equivalence handles retry port numbers decoded from legacy JSON.
		b, _ := json.Marshal(before[key])
		a, _ := json.Marshal(after[key])
		if string(b) != string(a) {
			reply.Differences = append(reply.Differences, Difference{key, before[key], after[key]})
		}
	}
	reply.Status, reply.Workflow = "match", &previous
	if len(reply.Differences) != 0 {
		reply.Status = "confirmation-required"
	}
	return reply, nil
}
func (s Service) Record(ctx context.Context, token string, request RecordRequest) (Record, error) {
	n, err := name(request.Name)
	if err != nil {
		return Record{}, err
	}
	recipe, fields, err := ValidateRecipe(request.Recipe)
	if err != nil {
		return Record{}, err
	}
	if request.Source == "" {
		request.Source = "manual"
	}
	if request.Source != "manual" && request.Source != "cli-success" {
		return Record{}, invalid("workflow source must be manual or cli-success")
	}
	if request.Note != nil && utf8.RuneCountInString(*request.Note) > 8000 {
		return Record{}, invalid("workflow note must be text (maximum 8000 characters)")
	}
	if request.ExpectedRevision != nil && *request.ExpectedRevision < 0 {
		return Record{}, invalid("invalid expected workflow revision")
	}
	evidence := map[string]any{}
	for _, key := range []string{"buildId", "image", "revision"} {
		var value string
		if json.Unmarshal(request.Evidence[key], &value) != nil {
			continue
		}
		value, err = SafeURL(value)
		if err != nil {
			return Record{}, err
		}
		runes := []rune(value)
		evidence[key] = string(runes[:min(len(runes), 4096)])
	}
	return s.Repository.UpdateWorkflow(ctx, token, func(c Catalog) (Record, error) {
		previous := c.Records[Slug(n)]
		if request.ExpectedRevision != nil && *request.ExpectedRevision != previous.Revision {
			return Record{}, ErrConflict
		}
		sel, err := selector(c, request.Selector, fields)
		if err != nil {
			return Record{}, err
		}
		now := time.Now()
		if s.Now != nil {
			now = s.Now()
		}
		r := Record{Name: n, Slug: Slug(n), Recipe: recipe, Source: request.Source, RepoKey: sel.RepoKey, Note: previous.Note, UpdatedAt: now.Unix(), Revision: previous.Revision + 1, LastSuccess: previous.LastSuccess}
		if r.Revision <= 0 {
			return Record{}, invalid("workflow revision overflow")
		}
		if r.RepoKey == "" {
			r.RepoKey = previous.RepoKey
		}
		if request.Note != nil {
			r.Note = *request.Note
		}
		if request.Source == "cli-success" {
			evidence["at"], evidence["recipe"] = r.UpdatedAt, recipe
			r.LastSuccess, err = json.Marshal(evidence)
		}
		return r, err
	})
}
