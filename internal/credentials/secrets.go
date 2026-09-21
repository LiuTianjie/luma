package credentials

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"regexp"
	"slices"
	"strings"
)

var ErrSecretInput = errors.New("invalid secret request")
var secretName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
var secretReference = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)\}`)
var scopeCharacters = regexp.MustCompile(`[^a-z0-9-]+`)

func ValidSecretName(name string) bool { return secretName.MatchString(name) }
func SecretScope(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", nil
	}
	scope := strings.Trim(scopeCharacters.ReplaceAllString(strings.ToLower(raw), "-"), "-")
	if scope == "" || len(scope) > 256 {
		return "", fmt.Errorf("%w: invalid application scope", ErrSecretInput)
	}
	return scope, nil
}

type SecretChange struct {
	Name  string  `json:"name"`
	Scope string  `json:"scope,omitempty"`
	Value *string `json:"value,omitempty"`
}
type SecretResult struct {
	Name    string `json:"name"`
	Scope   string `json:"scope"`
	Saved   *bool  `json:"saved,omitempty"`
	Removed *bool  `json:"removed,omitempty"`
}

func ValidateSecretChange(c SecretChange, remove bool) (SecretChange, error) {
	c.Name = strings.TrimSpace(c.Name)
	if !ValidSecretName(c.Name) {
		return c, fmt.Errorf("%w: secret name must be a valid environment variable name", ErrSecretInput)
	}
	var err error
	c.Scope, err = SecretScope(c.Scope)
	if err != nil {
		return c, err
	}
	if !remove && (c.Value == nil || *c.Value == "") {
		return c, fmt.Errorf("%w: secret value is required", ErrSecretInput)
	}
	if c.Value != nil && len(*c.Value) > 1<<20 {
		return c, fmt.Errorf("%w: secret value exceeds 1 MiB", ErrSecretInput)
	}
	return c, nil
}

type SecretRepository interface {
	SecretNames(context.Context) ([]string, error)
	ChangeSecret(context.Context, string, SecretChange, bool) (SecretResult, error)
}
type SecretSnapshot struct {
	Global, Scoped map[string]string
	HasScoped      bool
}
type SecretReader interface {
	ReadDeploymentSecrets(context.Context, string, []string) (SecretSnapshot, error)
}
type SecretInfo struct {
	Scope      string   `json:"scope"`
	Scoped     bool     `json:"scoped"`
	Imported   int      `json:"imported"`
	Referenced []string `json:"referenced"`
}
type SecretResolution struct {
	Values  map[string]string `json:"-"`
	Imports map[string]string `json:"-"`
	Info    SecretInfo
}

func SecretReferences(texts ...string) []string {
	names := map[string]bool{}
	for _, text := range texts {
		for _, match := range secretReference.FindAllStringSubmatch(text, -1) {
			names[match[1]] = true
		}
	}
	result := slices.Collect(maps.Keys(names))
	slices.Sort(result)
	return result
}
func ResolveSecrets(scope string, names, required []string, incoming map[string]string, snapshot SecretSnapshot) (SecretResolution, error) {
	r := SecretResolution{Values: maps.Clone(snapshot.Global), Imports: map[string]string{}, Info: SecretInfo{Scope: scope, Scoped: incoming != nil || snapshot.HasScoped, Referenced: slices.Clone(names)}}
	if r.Values == nil {
		r.Values = map[string]string{}
	}
	current := maps.Clone(snapshot.Scoped)
	if current == nil {
		current = map[string]string{}
	}
	for name, value := range incoming {
		if !ValidSecretName(name) {
			return r, fmt.Errorf("%w: invalid env secret name", ErrSecretInput)
		}
		if len(value) > 1<<20 {
			return r, fmt.Errorf("%w: env secret exceeds 1 MiB", ErrSecretInput)
		}
		if slices.Contains(names, name) {
			r.Imports[name] = value
			current[name] = value
		}
	}
	r.Info.Imported = len(r.Imports)
	wanted := required
	if r.Info.Scoped {
		wanted = names
		for _, name := range names {
			delete(r.Values, name)
			if value, ok := current[name]; ok {
				r.Values[name] = value
			}
		}
	}
	missing := []string{}
	for _, name := range wanted {
		if !ValidSecretName(name) {
			return r, fmt.Errorf("%w: invalid required secret name", ErrSecretInput)
		}
		if _, ok := r.Values[name]; !ok {
			missing = append(missing, name)
		}
	}
	if len(missing) > 0 {
		mode := "deployment"
		if r.Info.Scoped {
			mode = "scoped deployment"
		}
		return r, fmt.Errorf("%w: missing %s secrets for %s: %s", ErrSecretInput, mode, scope, strings.Join(missing, ", "))
	}
	return r, nil
}
func SubstituteSecrets(text string, values map[string]string) (string, error) {
	var missing string
	result := secretReference.ReplaceAllStringFunc(text, func(ref string) string {
		name := ref[2 : len(ref)-1]
		value, ok := values[name]
		if !ok {
			missing = name
		}
		return value
	})
	if missing != "" {
		return "", fmt.Errorf("%w: missing deployment secret: %s", ErrSecretInput, missing)
	}
	return result, nil
}
func SecretSetupKind(name string) string {
	return map[string]string{"CLOUDFLARE_API_TOKEN": "cloudflare", "CLOUDFLARE_ZONE_ID": "cloudflare", "TAILSCALE_AUTHKEY": "tailscale", "EGRESS_SUBSCRIPTION_URL": "egress", "TRAEFIK_ACME_EMAIL": "acme"}[name]
}
