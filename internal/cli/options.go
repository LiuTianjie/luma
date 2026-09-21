package cli

import (
	"flag"
	"fmt"
	"github.com/LiuTianjie/luma/internal/adapters/controlhttp"
	"io"
	"strings"
)

type options struct {
	localRepoURL, localBuilder, localProxy                                                            string
	buildNodes, directEgressNodes                                                                     []string
	defaultBuildNode, pushHost                                                                        string
	clearDirectEgress                                                                                 bool
	providerID, repository, buildNode, buildRef, buildRegion, buildExposure, buildDomain              string
	buildManifest, composeSidecar, buildPlatform, buildContext, dockerfile, registryHost, proxyMode   string
	buildPort                                                                                         int
	historyLimit                                                                                      int
	historyCursor, historyAfter, historyApp, historyStatus, historySource, historySince, historyUntil string
	registryUsername                                                                                  string
	registryPasswordStdin                                                                             bool
	secretScope, secretValue                                                                          string
	secretValueStdin                                                                                  bool
	config, format, endpoint, token, resolveIP, contextName, engine                                   string
	dryRun, insecure, skipDNS, acceptWorkflowChange, tokenStdin, local                                bool
	timeout                                                                                           int
	envFile, deployEnvFile                                                                            string
	noEnv                                                                                             bool
	used                                                                                              map[string]bool
	path, workflowNote, expectedName                                                                  string
	configRoot                                                                                        string
	lockedConnection                                                                                  *controlhttp.Options
}

func parse(args []string, errout io.Writer) (options, []string, error) {
	o := options{}
	f := flag.NewFlagSet("luma-go", flag.ContinueOnError)
	f.SetOutput(errout)
	f.StringVar(&o.localRepoURL, "repo-url", "", "local project repository URL")
	f.StringVar(&o.localBuilder, "builder", "", "existing local Buildx builder")
	f.StringVar(&o.localProxy, "proxy", "", "local build HTTP proxy")
	f.Func("node", "declared builder node (repeatable)", func(value string) error { o.buildNodes = append(o.buildNodes, value); return nil })
	f.Func("direct-egress-node", "builder node using direct internet access (repeatable)", func(value string) error { o.directEgressNodes = append(o.directEgressNodes, value); return nil })
	f.StringVar(&o.defaultBuildNode, "default-node", "", "default builder node")
	f.StringVar(&o.pushHost, "push-host", "", "registry address used for build pushes")
	f.BoolVar(&o.clearDirectEgress, "clear-direct-egress", false, "clear direct-egress builder nodes")
	f.StringVar(&o.providerID, "provider-id", "", "saved Git provider")
	f.StringVar(&o.repository, "repository", "", "provider owner/repository")
	f.StringVar(&o.buildNode, "build-node", "", "build node override")
	f.StringVar(&o.buildRef, "ref", "", "Git branch, tag or commit")
	f.StringVar(&o.buildRegion, "region", "", "deployment region override")
	f.StringVar(&o.buildExposure, "exposure", "", "native service exposure override")
	f.StringVar(&o.buildDomain, "domain", "", "native service domain override")
	f.IntVar(&o.buildPort, "port", 0, "native service port override")
	f.StringVar(&o.buildManifest, "manifest", "", "local deployment manifest override")
	f.StringVar(&o.composeSidecar, "compose-sidecar", "", "repository-relative Compose sidecar")
	f.StringVar(&o.buildPlatform, "platform", "", "container build platform")
	f.StringVar(&o.buildContext, "context", "", "repository-relative build context")
	f.StringVar(&o.dockerfile, "dockerfile", "", "repository-relative Dockerfile")
	f.StringVar(&o.registryHost, "registry-host", "", "registry pull host")
	f.StringVar(&o.proxyMode, "proxy-mode", "auto", "auto or direct builder networking")
	f.IntVar(&o.historyLimit, "limit", 50, "history page size (1-100)")
	f.StringVar(&o.historyCursor, "cursor", "", "history page cursor")
	f.StringVar(&o.historyAfter, "after", "", "build event resume position")
	f.StringVar(&o.historyApp, "app", "", "application filter")
	f.StringVar(&o.historyStatus, "status", "", "build status filter")
	f.StringVar(&o.historySource, "source", "", "history source filter")
	f.StringVar(&o.historySince, "since", "", "history start time")
	f.StringVar(&o.historyUntil, "until", "", "history end time")
	f.StringVar(&o.registryUsername, "username", "", "registry username")
	f.BoolVar(&o.registryPasswordStdin, "password-stdin", false, "read registry password from stdin")
	f.StringVar(&o.secretScope, "scope", "", "application secret scope")
	f.StringVar(&o.secretValue, "value", "", "secret value (prefer --value-stdin)")
	f.BoolVar(&o.secretValueStdin, "value-stdin", false, "read secret value from stdin")
	f.StringVar(&o.config, "config", "", "cluster YAML configuration")
	f.StringVar(&o.format, "format", "text", "text, json, or ndjson")
	f.StringVar(&o.endpoint, "control-url", "", "Control HTTPS endpoint")
	f.StringVar(&o.token, "token", "", "management token (prefer LUMA_DEPLOY_TOKEN or login --token-stdin)")
	f.StringVar(&o.resolveIP, "resolve-ip", "", "explicit Control IP (requires --insecure)")
	f.StringVar(&o.contextName, "control-context", "", "saved context name")
	f.StringVar(&o.engine, "engine", "", "orchestrator: nomad")
	f.BoolVar(&o.dryRun, "dry-run", false, "render without submitting")
	f.BoolVar(&o.insecure, "insecure", false, "disable TLS certificate verification")
	f.BoolVar(&o.skipDNS, "skip-dns", false, "skip DNS synchronization")
	f.BoolVar(&o.acceptWorkflowChange, "accept-workflow-change", false, "accept reviewed deployment workflow changes")
	f.BoolVar(&o.tokenStdin, "token-stdin", false, "read login token from stdin")
	f.BoolVar(&o.local, "local", false, "show local version only")
	f.IntVar(&o.timeout, "timeout", 1800, "deployment total deadline in seconds")
	f.StringVar(&o.envFile, "env-file", ".env", "local connection environment file")
	f.StringVar(&o.deployEnvFile, "env", "", "scoped deployment secrets file")
	f.StringVar(&o.deployEnvFile, "secrets-env-file", "", "alias of --env")
	f.BoolVar(&o.noEnv, "no-env", false, "do not load local connection environment")
	f.StringVar(&o.path, "path", ".", "workflow checkout directory")
	f.StringVar(&o.workflowNote, "workflow-note", "", "workflow explanation (no secrets)")
	f.StringVar(&o.workflowNote, "note", "", "workflow record explanation (no secrets)")
	// Go's standard flag parser stops at the first positional. Normalize known
	// options so the Python CLI's before/after-subcommand spellings both work.
	var flags, positionals []string
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if arg == "--" {
			positionals = append(positionals, args[i+1:]...)
			break
		}
		if !strings.HasPrefix(arg, "-") {
			positionals = append(positionals, arg)
			continue
		}
		name := strings.TrimLeft(strings.SplitN(arg, "=", 2)[0], "-")
		if name == "h" || name == "help" {
			return o, nil, flag.ErrHelp
		}
		known := f.Lookup(name)
		if known == nil {
			return o, nil, fmt.Errorf("unsupported option: %s", arg)
		}
		flags = append(flags, arg)
		if !strings.Contains(arg, "=") {
			if b, ok := known.Value.(interface{ IsBoolFlag() bool }); !ok || !b.IsBoolFlag() {
				i++
				if i >= len(args) {
					return o, nil, fmt.Errorf("option --%s requires a value", name)
				}
				flags = append(flags, args[i])
			}
		}
	}
	if err := f.Parse(flags); err != nil {
		return o, nil, err
	}
	if o.format != "text" && o.format != "json" && o.format != "ndjson" {
		return o, nil, fmt.Errorf("format must be text, json, or ndjson")
	}
	if o.timeout < 1 || o.timeout > 86400 {
		return o, nil, fmt.Errorf("timeout must be between 1 and 86400 seconds")
	}
	o.used = map[string]bool{}
	f.Visit(func(f *flag.Flag) { o.used[f.Name] = true })
	if (len(positionals) > 0 && positionals[0] == "import" || len(positionals) > 1 && positionals[0] == "build" && positionals[1] == "retry") && !o.used["timeout"] {
		o.timeout = 3600
	}
	if len(positionals) > 1 && positionals[0] == "build" && positionals[1] == "local" && !o.used["timeout"] {
		o.timeout = 7200
	}
	return o, positionals, nil
}

func validateOptions(args []string, o options) error {
	command := args[0]
	if (command == "compose" || command == "workflow" || command == "secret" || command == "registry" || command == "build") && len(args) > 1 {
		command += " " + args[1]
	}
	allowed := map[string]string{
		"username": "registry login", "password-stdin": "registry login",
		"scope": "secret set,secret import,secret remove", "value": "secret set", "value-stdin": "secret set",
		"config":  "validate,render,deploy,compose validate,compose render,compose deploy",
		"engine":  "validate,render,deploy,compose validate,compose render,compose deploy",
		"dry-run": "deploy,compose deploy", "skip-dns": "deploy,compose deploy",
		"accept-workflow-change": "deploy,compose deploy,workflow run,import,build retry,build local", "timeout": "deploy,compose deploy,import,build retry,build local",
		"env": "deploy,compose deploy,import,build retry,build local", "secrets-env-file": "deploy,compose deploy,import,build retry,build local",
		"token-stdin": "login", "local": "version",
		"path": "workflow run", "note": "workflow record", "workflow-note": "deploy,compose deploy,workflow run,import,build retry,build local",
	}
	for _, name := range strings.Fields("provider-id repository build-node ref region exposure domain port manifest compose-sidecar platform context dockerfile registry-host proxy-mode") {
		allowed[name] = "import"
	}
	for _, name := range strings.Fields("repo-url builder proxy compose-sidecar region exposure domain port platform context dockerfile") {
		if allowed[name] != "" {
			allowed[name] += ",build local"
		} else {
			allowed[name] = "build local"
		}
	}
	allowed["registry-host"] = "import,build config"
	for _, name := range strings.Fields("node default-node push-host direct-egress-node clear-direct-egress") {
		allowed[name] = "build config"
	}
	for _, name := range strings.Fields("limit cursor") {
		allowed[name] = "build list,build logs"
	}
	allowed["after"] = "build logs"
	for _, name := range strings.Fields("app status source since until") {
		allowed[name] = "build list"
	}
	for option, commands := range allowed {
		if o.used[option] {
			found := false
			for _, value := range strings.Split(commands, ",") {
				found = found || value == command
			}
			if !found {
				return fmt.Errorf("--%s is not supported by %s", option, command)
			}
		}
	}
	if o.noEnv && o.used["env-file"] {
		return fmt.Errorf("--no-env cannot be combined with --env-file")
	}
	return nil
}

const help = `Luma Go migration CLI

  luma-go validate MANIFEST [--config luma.yaml] [--format json]
  luma-go render MANIFEST [--config luma.yaml]
  luma-go deploy MANIFEST [--dry-run] [--format text|json|ndjson]
  luma-go compose validate|render|deploy SIDECAR [--dry-run]
  luma-go import REPOSITORY [--ref REF] [--compose-sidecar PATH] [--env FILE]
  luma-go build local [PATH] [--repo-url URL] [--builder NAME] [--env FILE]
  luma-go build config [--node NAME] [--default-node NAME] [--registry-host HOST] [--push-host HOST]
  luma-go build list | build logs ID | build cancel ID | build retry ID
  luma-go login HTTPS_URL --token-stdin
  luma-go context list | context use NAME
  luma-go workflow list | workflow show NAME
  luma-go workflow run NAME [--path CHECKOUT]
  luma-go workflow record NAME [--note TEXT] -- deploy MANIFEST [OPTIONS]
  luma-go secret list | secret set NAME [--scope APP] --value-stdin
  luma-go secret import ENV_FILE --scope APP | secret remove NAME [--scope APP]
  luma-go registry list | registry login HOST --username USER --password-stdin
  luma-go registry remove HOST
  luma-go status [--control-context NAME] [--format json]
  luma-go version [--local]

Uses the existing LUMA_CONFIG_HOME contexts, LUMA_CONTROL_URL and
LUMA_DEPLOY_TOKEN. Deployment uses the existing Control API and workflow guard.
Deploy accepts --env FILE for scoped secrets; --env-file FILE configures local
connection settings. Compose deploy (including dry-run) loads Control inventory.
Local builds, build retries, bootstrap and node management remain pending. No implicit fallback.
Local previews preserve secret placeholders and do not verify cluster readiness.
`
