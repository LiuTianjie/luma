// Package workflow owns portable deployment recipes and optimistic revision
// checks. Recipes are data: this package never executes commands or reads files.
package workflow

import (
	"encoding/json"
	"fmt"
	"maps"
	"net/url"
	"path"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"
)

type Recipe struct {
	SchemaVersion   int            `json:"schemaVersion"`
	Method          string         `json:"method"`
	Argv            []string       `json:"argv"`
	RetryParameters map[string]any `json:"retryParameters,omitempty"`
}

// SafeURL removes authentication and query/fragment data before persistence.
// No networking is performed; net/url is used only as a pure syntax parser.
func SafeURL(value string) (string, error) {
	if !strings.Contains(value, "://") {
		return value, nil
	}
	u, err := url.Parse(value)
	if err != nil {
		return "", invalid("invalid URL in workflow")
	}
	u.User, u.RawQuery, u.Fragment, u.RawFragment = nil, "", "", ""
	u.ForceQuery = false
	return u.String(), nil
}

var scpURL = regexp.MustCompile(`^[^/]+@[^:]+:`)

func RepoKey(value string) (string, error) {
	value, err := SafeURL(strings.TrimSpace(value))
	if err != nil || value == "" {
		return "", err
	}
	if !strings.Contains(value, "://") && scpURL.MatchString(value) {
		value = "ssh://" + strings.Replace(value, ":", "/", 1)
	}
	if !strings.Contains(value, "://") {
		value = "https://github.com/" + value
	}
	u, err := url.Parse(value)
	if err != nil || u.Hostname() == "" {
		return "", invalid("invalid workflow repository")
	}
	return strings.ToLower(u.Hostname() + "/" + strings.TrimSuffix(strings.Trim(u.Path, "/"), ".git")), nil
}

type option struct{ field, methods string }

var recipeOptions = map[string]option{
	"config": {}, "env-file": {}, "no-env": {}, "timeout": {}, "format": {},
	"env": {"deploy_env_file", ""}, "secrets-env-file": {"deploy_env_file", "image-deploy compose-deploy remote-build local-build"},
	"skip-dns": {"skip_dns", "image-deploy compose-deploy"}, "engine": {"engine", "image-deploy compose-deploy"},
	"provider-id": {"provider_id", "remote-build"}, "repository": {"repository", "remote-build"},
	"build-node": {"build_node", "remote-build"}, "ref": {"ref", "remote-build"},
	"region": {"region", "remote-build local-build"}, "exposure": {"exposure", "remote-build local-build"},
	"domain": {"domain", "remote-build local-build"}, "port": {"port", "remote-build local-build"},
	"manifest": {"manifest", "remote-build"}, "compose-sidecar": {"compose_sidecar", "remote-build local-build"},
	"platform": {"platform", "remote-build local-build"}, "context": {"build_context", "remote-build local-build"},
	"dockerfile": {"dockerfile", "remote-build local-build"}, "registry-host": {"registry_host", "remote-build"},
	"proxy-mode": {"proxy_mode", "remote-build"}, "repo-url": {"repo_url", "local-build"},
	"builder": {"builder", "local-build"}, "proxy": {"proxy", "local-build"},
}

// ValidateRecipe has an explicit grammar independent of the interactive CLI.
// Adding an unrelated CLI option can never silently expand stored recipes.
func ValidateRecipe(r Recipe) (Recipe, map[string]any, error) {
	if r.SchemaVersion != 1 || len(r.Argv) == 0 || len(r.Argv) > 128 {
		return Recipe{}, nil, invalid("unsupported deployment workflow schema or argv")
	}
	r.Argv = slices.Clone(r.Argv)
	pos := []string{}
	opts := map[string]string{}
	flags := map[string]bool{}
	positionalsOnly := false
	for i := 0; i < len(r.Argv); i++ {
		arg := r.Argv[i]
		if !utf8.ValidString(arg) || utf8.RuneCountInString(arg) > 4096 || strings.ContainsRune(arg, 0) {
			return Recipe{}, nil, invalid("invalid workflow argument")
		}
		if arg == "--" && !positionalsOnly {
			positionalsOnly = true
			continue
		}
		if !strings.HasPrefix(arg, "-") || positionalsOnly {
			pos = append(pos, arg)
			continue
		}
		if !strings.HasPrefix(arg, "--") {
			return Recipe{}, nil, invalid("unsupported workflow option")
		}
		key, val, hasValue := strings.Cut(strings.TrimPrefix(arg, "--"), "=")
		if _, ok := recipeOptions[key]; !ok {
			return Recipe{}, nil, invalid("workflow cannot include --" + key)
		}
		if key == "no-env" || key == "skip-dns" {
			if hasValue {
				return Recipe{}, nil, invalid("workflow boolean option cannot take a value")
			}
			flags[key] = true
			continue
		}
		if !hasValue {
			i++
			if i >= len(r.Argv) || strings.HasPrefix(r.Argv[i], "--") {
				return Recipe{}, nil, invalid("workflow option requires a value")
			}
			val = r.Argv[i]
		}
		if utf8.RuneCountInString(val) > 4096 || strings.ContainsRune(val, 0) || !utf8.ValidString(val) {
			return Recipe{}, nil, invalid("invalid workflow argument")
		}
		// Canonicalize aliases in encounter order, matching the CLI's last value.
		if key == "secrets-env-file" {
			key = "env"
		}
		opts[key] = val
	}
	method, retry := "", false
	fields := map[string]any{}
	switch {
	case len(pos) == 2 && pos[0] == "deploy":
		method, fields["service"] = "image-deploy", pythonPath(pos[1])
	case len(pos) == 3 && pos[0] == "compose" && pos[1] == "deploy":
		method, fields["sidecar"] = "compose-deploy", pythonPath(pos[2])
	case len(pos) >= 1 && len(pos) <= 2 && pos[0] == "import":
		method = "remote-build"
		if len(pos) == 2 && pos[1] != "" {
			fields["repo"] = pos[1]
		}
	case len(pos) >= 2 && len(pos) <= 3 && pos[0] == "build" && pos[1] == "local":
		method, fields["path"] = "local-build", "."
		if len(pos) == 3 {
			fields["path"] = pythonPath(pos[2])
		}
	case len(pos) == 3 && pos[0] == "build" && pos[1] == "retry":
		method, retry = "remote-build", true
	default:
		return Recipe{}, nil, invalid("workflow must contain a deployment command")
	}
	if r.Method != method {
		return Recipe{}, nil, invalid("workflow method does not match its command")
	}
	for key, value := range opts {
		o := recipeOptions[key]
		if o.methods != "" && !slices.Contains(strings.Fields(o.methods), method) || retry && !slices.Contains([]string{"config", "env-file", "timeout", "env", "format"}, key) {
			return Recipe{}, nil, invalid("workflow option is not supported for this command")
		}
		if key == "engine" && value != "nomad" || key == "proxy-mode" && value != "auto" && value != "direct" || key == "format" && value != "text" && value != "json" && value != "ndjson" {
			return Recipe{}, nil, invalid("invalid workflow option value")
		}
		if key == "port" || key == "timeout" {
			n, err := strconv.Atoi(value)
			if err != nil {
				return Recipe{}, nil, invalid("workflow port/timeout must be an integer")
			}
			if key == "port" && n != 0 {
				fields[o.field] = n
			}
		} else if o.field != "" && value != "" {
			if key == "env" || key == "manifest" {
				value = pythonPath(value)
			}
			fields[o.field] = value
		}
	}
	if flags["skip-dns"] {
		if method != "image-deploy" && method != "compose-deploy" {
			return Recipe{}, nil, invalid("skip-dns is not supported for this workflow")
		}
		fields["skip_dns"] = true
	}
	fields["method"] = method
	if retry {
		var err error
		fields, err = retryFields(r.RetryParameters)
		if err != nil {
			return Recipe{}, nil, err
		}
		r.RetryParameters = maps.Clone(fields)
	} else {
		r.RetryParameters = nil
	}
	for key, value := range fields {
		if s, ok := value.(string); ok {
			clean, err := SafeURL(s)
			if err != nil {
				return Recipe{}, nil, err
			}
			fields[key] = clean
		}
	}
	for key, value := range map[string]string{"build_context": ".", "dockerfile": "Dockerfile", "proxy_mode": "auto"} {
		if fields[key] == value {
			delete(fields, key)
		}
	}
	for i, arg := range r.Argv {
		// --repo-url=https://... must sanitize the value, not treat the flag as a URL scheme.
		prefix, value, found := strings.Cut(arg, "=")
		if !strings.HasPrefix(arg, "--") || !found {
			prefix, value = "", arg
		}
		clean, err := SafeURL(value)
		if err != nil {
			return Recipe{}, nil, err
		}
		if prefix != "" {
			clean = prefix + "=" + clean
		}
		r.Argv[i] = clean
	}
	return r, fields, nil
}

// pathlib.Path drops '.' and redundant separators but preserves '..'. Avoid
// path.Clean here: collapsing '..' can change paths that traverse a symlink.
func pythonPath(value string) string {
	parts := []string{}
	for _, part := range strings.Split(value, "/") {
		if part != "" && part != "." {
			parts = append(parts, part)
		}
	}
	prefix := ""
	if path.IsAbs(value) {
		prefix = "/"
	}
	if strings.HasPrefix(value, "//") && !strings.HasPrefix(value, "///") {
		prefix = "//"
	}
	result := prefix + strings.Join(parts, "/")
	if result == "" {
		result = "."
	}
	return result
}

func retryFields(input map[string]any) (map[string]any, error) {
	if len(input) > 32 || input["method"] != "remote-build" {
		return nil, invalid("retry workflow requires its recorded build parameters")
	}
	allowed := strings.Fields("method repo provider_id repository ref build_node region exposure domain port compose_sidecar platform build_context dockerfile registry_host proxy_mode manifest deploy_env_file")
	result := map[string]any{}
	for key, value := range input {
		if !slices.Contains(allowed, key) {
			return nil, invalid("unknown retry workflow parameter")
		}
		switch v := value.(type) {
		case string:
			if utf8.RuneCountInString(v) > 4096 || strings.ContainsRune(v, 0) {
				return nil, invalid("invalid retry workflow parameter")
			}
			clean, err := SafeURL(v)
			if err != nil {
				return nil, err
			}
			result[key] = clean
		case json.Number:
			if _, err := v.Int64(); err != nil {
				return nil, invalid("retry workflow numbers must be integers")
			}
			result[key] = v
		case int, int64:
			result[key] = v
		case float64:
			if v != float64(int64(v)) {
				return nil, invalid("retry workflow numbers must be integers")
			}
			result[key] = int64(v)
		default:
			return nil, invalid(fmt.Sprintf("invalid retry workflow parameter: %s", key))
		}
	}
	return result, nil
}
