# Luma CLI 命令参考 {#luma-cli-command-reference}

<!-- 由 scripts/generate-cli-reference.py 生成，请勿手动编辑。 -->

本页由当前 CLI 解析器自动生成，帮助文本保持英文原样，便于与终端输出逐项核对。
使用 `python scripts/generate-cli-reference.py` 重新生成，CI 通过 `--check` 校验。
按任务组织的说明见[快速上手](getting-started.zh-CN.md)、[部署应用](deploying.zh-CN.md)和[日常运维](operations.zh-CN.md)。
全局选项写在命令之前，命令选项写在命令之后。

## `luma`

```text
usage: luma <command> [options]

Deploy containers to your own servers through Luma Control.

Get started:
  init          Create a service manifest
  login         Save a Control endpoint and management token
  context       List or switch saved Control logins
  doctor        Check this machine, Control and nodes
  version       Show CLI and Control versions

Deploy:
  validate      Check a service manifest without contacting Control
  deploy        Deploy a service manifest
  compose       Deploy a Docker Compose application with a Luma sidecar
  import        Build a Git repository on a builder node and deploy it
  build         Build a local checkout, or inspect and retry build runs
  workflow      Show or record how an application is built and deployed

Operate:
  status        Show cluster, node and application health
  app           Inspect, restart, roll back or remove deployed applications
  secret        Manage application secrets

Cluster administration:
  bootstrap     Install the Luma manager on this machine
  update        Update the CLI, the manager or every node
  node          Join, list and remove nodes
  region        Manage scheduling regions
  storage       Manage NFS storage classes
  registry      Manage registry credentials and the managed registry
  git-provider  Manage saved GitHub and Gitea credentials
  manager       Repair manager networking, DNS and IP changes

arguments:
  -h, --help           show this help message and exit
  --version            show program's version number and exit
  --config CONFIG      Cluster config file, used on the manager (default: ./luma.yaml if present)
  --env-file ENV_FILE  Load environment variables from this file (default: Luma settings from
                       ./.env)
  --no-env             Do not load ./.env or saved local settings

Run 'luma <command> --help' for the options of a command.
Documentation: https://liutianjie.github.io/luma/
```

## `luma init`

```text
usage: luma init [-h] [--name NAME] [--image IMAGE] [--region REGION] [--exposure {cloudflare-tunnel,cn-edge,external-edge,none,tailscale-relay,tcp-relay}] [--domain DOMAIN] [--port PORT] [--replicas REPLICAS] [--output OUTPUT] [--force]

Create a service manifest. Prompts for missing values in a terminal; without a terminal every value
comes from flags or defaults.

arguments:
  -h, --help                  show this help message and exit
  --name NAME                 Application name (default: app)
  --image IMAGE               Container image (default: ghcr.io/your-org/<name>:latest)
  --region REGION             Where the service runs: cn, global, home or a created region (default:
                              cn)
  --exposure {cloudflare-tunnel,cn-edge,external-edge,none,tailscale-relay,tcp-relay}
                              How traffic reaches the service (default: cn-edge)
  --domain DOMAIN             Public hostname for edge and tunnel exposures
  --port PORT                 Container port (default: 3000)
  --replicas REPLICAS         Number of instances (default: 1)
  --output OUTPUT, -o OUTPUT  Manifest path (default: <name>.yaml)
  --force                     Overwrite an existing manifest

Example: luma init --name web --image nginx:1.27 --region cn --exposure cn-edge --domain
web.example.com --port 80
```

## `luma login`

```text
usage: luma login [-h] [--token TOKEN | --token-stdin] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] endpoint

Save a Control endpoint and management token

arguments:
  endpoint                    Control URL, for example https://luma.example.com

arguments:
  -h, --help                  show this help message and exit
  --token TOKEN               Management token (prefer --token-stdin or LUMA_DEPLOY_TOKEN)
  --token-stdin               Read the management token from stdin
  --insecure                  Skip TLS verification for self-signed endpoints
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the endpoint hostname as Host
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

Example: luma login https://luma.example.com --token-stdin < token.txt
```

## `luma context`

```text
usage: luma context [-h] <command> ...

List or switch saved Control logins

arguments:
  -h, --help    show this help message and exit

arguments:
  <command>
    list        List saved logins
    use         Switch the current login
```

## `luma context list`

```text
usage: luma context list [-h] [--format {text,json,ndjson}] [--quiet]

List saved logins

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error
```

## `luma context use`

```text
usage: luma context use [-h] [--format {text,json,ndjson}] [--quiet] cluster

Switch the current login

arguments:
  cluster                     Cluster ID or name shown by 'luma context list'

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error
```

## `luma doctor`

```text
usage: luma doctor [-h] [--local] [--deep] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

Check this machine, Control and nodes

arguments:
  -h, --help                  show this help message and exit
  --local                     Only inspect this machine's installation; do not contact Control
  --deep                      Also run slower live checks on every node
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma version`

```text
usage: luma version [-h] [--local] [--control-url CONTROL_URL] [--insecure] [--resolve-ip RESOLVE_IP]

Show CLI and Control versions

arguments:
  -h, --help                 show this help message and exit
  --local                    Only print the local CLI version
  --control-url CONTROL_URL  Control URL to check instead of the current login
  --insecure                 Skip TLS verification for the Control check
  --resolve-ip RESOLVE_IP    Connect to this IP while keeping the Control hostname as Host
```

## `luma validate`

```text
usage: luma validate [-h] [--format {text,json,ndjson}] [--quiet] FILE

Check a service manifest without contacting Control

arguments:
  FILE                        Service manifest

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

Use 'luma deploy FILE --dry-run' to see the rendered Nomad job.
```

## `luma deploy`

```text
usage: luma deploy [-h] [--dry-run] [--env DEPLOY_ENV_FILE] [--timeout TIMEOUT] [--skip-dns] [--skip-orchestrator] [--workflow-app WORKFLOW_APP] [--accept-workflow-change] [--workflow-note WORKFLOW_NOTE] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] FILE

Deploy a service manifest through Luma Control. The image must already exist; use 'luma build local'
or 'luma import' to build one.

arguments:
  FILE                        Service manifest

arguments:
  -h, --help                  show this help message and exit
  --dry-run                   Render the Nomad job locally and print it; nothing is deployed
  --env DEPLOY_ENV_FILE       Store the variables the manifest references from this .env file as
                              application secrets
  --timeout TIMEOUT           Seconds to wait for Control (default: 3000)
  --skip-dns                  Do not create or update Cloudflare DNS records
  --skip-orchestrator         Update routes and DNS without submitting the Nomad job
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --workflow-app WORKFLOW_APP
                              Application whose workflow to check, when a repository has several
  --accept-workflow-change    Proceed after reviewing the reported workflow differences
  --workflow-note WORKFLOW_NOTE
                              Record why this workflow is used; never include secrets

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host

Examples:
  luma deploy app.yaml --dry-run
  luma deploy app.yaml --env .env
```

## `luma compose`

```text
usage: luma compose [-h] <command> ...

Deploy a Docker Compose application with a Luma sidecar

arguments:
  -h, --help    show this help message and exit

arguments:
  <command>
    init        Create a Luma sidecar for a docker-compose.yml
    validate    Check a Compose sidecar and the Compose file it references
    deploy      Deploy a Compose application
```

## `luma compose init`

```text
usage: luma compose init [-h] [--compose COMPOSE] [--output OUTPUT]

Create a Luma sidecar for a docker-compose.yml

arguments:
  -h, --help         show this help message and exit
  --compose COMPOSE  Compose file (default: docker-compose.yml)
  --output OUTPUT    Sidecar path (default: luma.compose.yml)
```

## `luma compose validate`

```text
usage: luma compose validate [-h] [--import-mode] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] SIDECAR

Check a Compose sidecar and the Compose file it references

arguments:
  SIDECAR                     Luma Compose sidecar, for example luma.compose.yml

arguments:
  -h, --help                  show this help message and exit
  --import-mode               Allow services with build: as 'luma import' does
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma compose deploy`

```text
usage: luma compose deploy [-h] [--dry-run] [--env DEPLOY_ENV_FILE] [--timeout TIMEOUT] [--skip-dns] [--skip-orchestrator] [--workflow-app WORKFLOW_APP] [--accept-workflow-change] [--workflow-note WORKFLOW_NOTE] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] SIDECAR

Deploy a Compose application

arguments:
  SIDECAR                     Luma Compose sidecar, for example luma.compose.yml

arguments:
  -h, --help                  show this help message and exit
  --dry-run                   Render the Nomad job and print it; nothing is deployed
  --env DEPLOY_ENV_FILE       Store referenced variables from this .env file as application secrets
  --timeout TIMEOUT           Seconds to wait for Control (default: 3000)
  --skip-dns                  Do not create or update Cloudflare DNS records
  --skip-orchestrator         Update routes and DNS without submitting the Nomad job
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --workflow-app WORKFLOW_APP
                              Application whose workflow to check, when a repository has several
  --accept-workflow-change    Proceed after reviewing the reported workflow differences
  --workflow-note WORKFLOW_NOTE
                              Record why this workflow is used; never include secrets

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma import`

```text
usage: luma import [-h] [--ref REF] [--provider-id PROVIDER_ID] [--repository REPOSITORY] [--manifest MANIFEST] [--compose-sidecar COMPOSE_SIDECAR] [--region REGION] [--exposure EXPOSURE] [--domain DOMAIN] [--port PORT] [--env DEPLOY_ENV_FILE] [--build-node BUILD_NODE] [--platform PLATFORM] [--context BUILD_CONTEXT] [--dockerfile DOCKERFILE] [--registry-host REGISTRY_HOST] [--proxy-mode {auto,direct}] [--timeout TIMEOUT] [--workflow-app WORKFLOW_APP] [--accept-workflow-change] [--workflow-note WORKFLOW_NOTE] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] [repo]

Build and deploy a Git repository on a builder node. Luma looks for a .luma.yml service manifest or
a luma.compose.yml sidecar; Compose services with build: are built, pushed to the internal registry
and deployed.

arguments:
  repo                        Repository URL or owner/name (GitHub); omit with --provider-id and
                              --repository

arguments:
  -h, --help                  show this help message and exit
  --ref REF                   Branch or tag to build (default: the repository's default branch)
  --provider-id PROVIDER_ID   Saved Git provider credential, for example github:personal
  --repository REPOSITORY     Repository full name for --provider-id, for example owner/name
  --manifest MANIFEST         Local manifest to use when the repository has none
  --compose-sidecar COMPOSE_SIDECAR
                              Repository-relative Compose sidecar to use instead of auto-discovery
  --region REGION             Override the manifest's region
  --exposure EXPOSURE         Override the manifest's exposure (single service)
  --domain DOMAIN             Override the manifest's domain (single service)
  --port PORT                 Override the manifest's container port (single service)
  --env DEPLOY_ENV_FILE       Store variables from this .env file as application secrets
  --build-node BUILD_NODE     Builder node to use instead of the default
  --platform PLATFORM         Build platform (default: linux/amd64 or the manifest's build.platform)
  --context BUILD_CONTEXT     Docker build context inside the repository (default: .)
  --dockerfile DOCKERFILE     Dockerfile inside the repository (default: Dockerfile)
  --registry-host REGISTRY_HOST
                              Registry host that nodes pull from (default: <build-node>:5000)
  --proxy-mode {auto,direct}  Builder network: auto follows the node's region policy, direct
                              disables the proxy
  --timeout TIMEOUT           Seconds to wait for build and deploy (default: 3600)
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --workflow-app WORKFLOW_APP
                              Application whose workflow to check, when a repository has several
  --accept-workflow-change    Proceed after reviewing the reported workflow differences
  --workflow-note WORKFLOW_NOTE
                              Record why this workflow is used; never include secrets

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host

Examples:
  luma import acme/web
  luma import https://gitea.example.com/acme/web.git --ref v1.2.0
```

## `luma build`

```text
usage: luma build [-h] <command> ...

Build a local checkout, or inspect and retry build runs

arguments:
  -h, --help    show this help message and exit

arguments:
  <command>
    local       Build a local checkout with Docker Buildx, push it and deploy it
    list        List recent build runs
    logs        Show the step log of a build run
    retry       Run a recorded build again
    cancel      Cancel a running build
    config      Show or set builder nodes and the internal registry
```

## `luma build local`

```text
usage: luma build local [-h] [--compose-sidecar COMPOSE_SIDECAR] [--region REGION] [--exposure EXPOSURE] [--domain DOMAIN] [--port PORT] [--platform PLATFORM] [--builder BUILDER] [--proxy PROXY] [--context BUILD_CONTEXT] [--dockerfile DOCKERFILE] [--repo-url REPO_URL] [--env DEPLOY_ENV_FILE] [--timeout TIMEOUT] [--workflow-app WORKFLOW_APP] [--accept-workflow-change] [--workflow-note WORKFLOW_NOTE] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] [path]

Build a local checkout with Docker Buildx, push it and deploy it

arguments:
  path                        Project directory (default: .)

arguments:
  -h, --help                  show this help message and exit
  --compose-sidecar COMPOSE_SIDECAR
                              Repository-relative Compose sidecar to use
  --region REGION             Override the manifest's region
  --exposure EXPOSURE         Override the manifest's exposure (single service)
  --domain DOMAIN             Override the manifest's domain (single service)
  --port PORT                 Override the manifest's container port (single service)
  --platform PLATFORM         Build platform, for example linux/amd64
  --builder BUILDER           Existing local Docker Buildx builder
  --proxy PROXY               HTTP proxy for base image pulls and RUN steps
  --context BUILD_CONTEXT     Docker build context inside the project
  --dockerfile DOCKERFILE     Dockerfile inside the project
  --repo-url REPO_URL         Project Git URL used to name the image (default: the origin remote)
  --env DEPLOY_ENV_FILE       Store variables from this .env file as application secrets
  --timeout TIMEOUT           Seconds allowed for build and deploy (default: 7200)
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --workflow-app WORKFLOW_APP
                              Application whose workflow to check, when a repository has several
  --accept-workflow-change    Proceed after reviewing the reported workflow differences
  --workflow-note WORKFLOW_NOTE
                              Record why this workflow is used; never include secrets

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host

Example: luma build local . --platform linux/amd64
```

## `luma build list`

```text
usage: luma build list [-h] [--limit LIMIT] [--cursor CURSOR] [--app APP] [--status STATUS] [--source {build,cli,dashboard}] [--since SINCE] [--until UNTIL] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

List recent build runs

arguments:
  -h, --help                  show this help message and exit
  --limit LIMIT               Records per page, 1-100 (default: 50)
  --cursor CURSOR             nextCursor from the previous page with the same filters
  --app APP                   Only this application
  --status STATUS             Only this recorded status
  --source {build,cli,dashboard}
                              Only records from this source
  --since SINCE               Created at or after (Unix seconds or RFC 3339)
  --until UNTIL               Created at or before (Unix seconds or RFC 3339)
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma build logs`

```text
usage: luma build logs [-h] [--limit LIMIT] [--cursor CURSOR] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] id

Show the step log of a build run

arguments:
  id                          Build run ID from 'luma build list'

arguments:
  -h, --help                  show this help message and exit
  --limit LIMIT               Records per page, 1-100 (default: 50)
  --cursor CURSOR             nextCursor from the previous page with the same filters
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma build retry`

```text
usage: luma build retry [-h] [--env DEPLOY_ENV_FILE] [--timeout TIMEOUT] [--workflow-app WORKFLOW_APP] [--accept-workflow-change] [--workflow-note WORKFLOW_NOTE] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] id

Run a recorded build again

arguments:
  id                          Build run ID from 'luma build list'

arguments:
  -h, --help                  show this help message and exit
  --env DEPLOY_ENV_FILE       Store variables from this .env file as application secrets
  --timeout TIMEOUT           Seconds to wait for build and deploy (default: 3600)
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --workflow-app WORKFLOW_APP
                              Application whose workflow to check, when a repository has several
  --accept-workflow-change    Proceed after reviewing the reported workflow differences
  --workflow-note WORKFLOW_NOTE
                              Record why this workflow is used; never include secrets

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma build cancel`

```text
usage: luma build cancel [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] id

Cancel a running build

arguments:
  id                          Build run ID from 'luma build list'

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma build config`

```text
usage: luma build config [-h] [--node NODES] [--default-node DEFAULT_NODE] [--registry-host REGISTRY_HOST] [--push-host PUSH_HOST] [--direct-egress-node DIRECT_EGRESS_NODES] [--clear-direct-egress] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

Show or set builder nodes and the internal registry

arguments:
  -h, --help                  show this help message and exit
  --node NODES                Builder node; repeat for several
  --default-node DEFAULT_NODE
                              Builder used by 'luma import' by default
  --registry-host REGISTRY_HOST
                              Registry host that nodes pull from, for example 10.0.0.5:5000
  --push-host PUSH_HOST       Registry host that builds push to (the builder's mesh address, not
                              localhost)
  --direct-egress-node DIRECT_EGRESS_NODES
                              Builder with direct internet access; repeat for several
  --clear-direct-egress       Clear the direct-egress builder list
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma workflow`

```text
usage: luma workflow [-h] <command> ...

Show or record how an application is built and deployed. Deploy, import and build commands compare
themselves with the recorded workflow and stop on changes.

arguments:
  -h, --help    show this help message and exit

arguments:
  <command>
    list        List recorded workflows
    show        Show an application's recorded command and notes
    record      Record a workflow without deploying
    run         Run the recorded command from a local checkout
```

## `luma workflow list`

```text
usage: luma workflow list [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

List recorded workflows

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma workflow show`

```text
usage: luma workflow show [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] name

Show an application's recorded command and notes

arguments:
  name                        Application name

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma workflow record`

```text
usage: luma workflow record [-h] [--note WORKFLOW_NOTE] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] name COMMAND [COMMAND ...]

Record a workflow without deploying

arguments:
  name                        Application name
  COMMAND                     The luma command, after --

arguments:
  -h, --help                  show this help message and exit
  --note WORKFLOW_NOTE        Why this workflow is used; never include secrets
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host

Example: luma workflow record web -- luma build local . --platform linux/amd64
```

## `luma workflow run`

```text
usage: luma workflow run [-h] [--path PATH] [--workflow-app WORKFLOW_APP] [--accept-workflow-change] [--workflow-note WORKFLOW_NOTE] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] name

Run the recorded command from a local checkout

arguments:
  name                        Application name

arguments:
  -h, --help                  show this help message and exit
  --path PATH                 Local project checkout (default: .)
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --workflow-app WORKFLOW_APP
                              Application whose workflow to check, when a repository has several
  --accept-workflow-change    Proceed after reviewing the reported workflow differences
  --workflow-note WORKFLOW_NOTE
                              Record why this workflow is used; never include secrets

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma status`

```text
usage: luma status [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

Show cluster, node and application health

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma app`

```text
usage: luma app [-h] <command> ...

Inspect, restart, roll back or remove deployed applications

arguments:
  -h, --help    show this help message and exit

arguments:
  <command>
    list        List deployed services and their replica health
    show        Show an application or one of its services
    logs        Read application logs
    events      Show recent runtime events of the latest allocation
    history     Page through build and deployment attempts
    versions    List the Nomad job versions that 'luma app rollback' can restore
    rollback    Restore a previous Nomad job version
    restart     Restart an application
    remove      Remove an application, its routes and DNS records

Examples:
  luma app list
  luma app logs web --follow
  luma app rollback web
```

## `luma app list`

```text
usage: luma app list [-h] [--region REGION] [--app STACK] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

List deployed services and their replica health

arguments:
  -h, --help                  show this help message and exit
  --region REGION             Only this region
  --app STACK                 Only this application
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma app show`

```text
usage: luma app show [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] name

Show an application or one of its services

arguments:
  name                        Application or full service name

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma app logs`

```text
usage: luma app logs [-h] [--follow] [--tail TAIL] [--previous] [--allocation ALLOCATION] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] name

Read application logs

arguments:
  name                        Full service name from 'luma app list'

arguments:
  -h, --help                  show this help message and exit
  --follow, -f                Keep streaming new lines until interrupted
  --tail TAIL                 Recent lines to read across all sources, 1-500 (default: 120)
  --previous                  Read stopped allocations instead of running ones
  --allocation ALLOCATION     Only this allocation ID
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma app events`

```text
usage: luma app events [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] name

Show recent runtime events of the latest allocation

arguments:
  name                        Full service name from 'luma app list'

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma app history`

```text
usage: luma app history [-h] [--kind {build,deployment}] [--id RECORD_ID] [--limit LIMIT] [--cursor CURSOR] [--status STATUS] [--source {build,cli,dashboard}] [--since SINCE] [--until UNTIL] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] [name]

Page through build and deployment attempts

arguments:
  name                        Only this application

arguments:
  -h, --help                  show this help message and exit
  --kind {build,deployment}   Only this record type, or the type of --id
  --id RECORD_ID              Show one record and its step log; requires --kind
  --limit LIMIT               Records per page, 1-100 (default: 50)
  --cursor CURSOR             nextCursor from the previous page with the same filters
  --status STATUS             Only this recorded status
  --source {build,cli,dashboard}
                              Only records from this source
  --since SINCE               Created at or after (Unix seconds or RFC 3339)
  --until UNTIL               Created at or before (Unix seconds or RFC 3339)
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma app versions`

```text
usage: luma app versions [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] name

List the Nomad job versions that 'luma app rollback' can restore

arguments:
  name                        Application name

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma app rollback`

```text
usage: luma app rollback [-h] [--to-version TO_VERSION] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] name

Restore a previous Nomad job version. This does not restore application data.

arguments:
  name                        Application name

arguments:
  -h, --help                  show this help message and exit
  --to-version TO_VERSION     Version from 'luma app versions' (default: the previous one)
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma app restart`

```text
usage: luma app restart [-h] [--service SERVICE] [--mode {recreate,task}] [--timeout TIMEOUT] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] name

Restart an application

arguments:
  name                        Application name

arguments:
  -h, --help                  show this help message and exit
  --service SERVICE           Only this service of a Compose application
  --mode {recreate,task}      recreate replaces the allocation; task restarts in place
  --timeout TIMEOUT           Seconds to wait for Control (default: 120)
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma app remove`

```text
usage: luma app remove [-h] [--dry-run] [--delete-storage] [--skip-dns] [--skip-orchestrator] [--timeout TIMEOUT] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] name

Remove an application, its routes and DNS records

arguments:
  name                        Application name

arguments:
  -h, --help                  show this help message and exit
  --dry-run                   Show what would be removed
  --delete-storage            Also delete removable storage recorded for the deployment
  --skip-dns                  Keep Cloudflare DNS records
  --skip-orchestrator         Keep the Nomad job running
  --timeout TIMEOUT           Seconds to wait for Control (default: 300)
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma secret`

```text
usage: luma secret [-h] <command> ...

Manage application secrets. Manifests reference them as ${NAME}; the scope is the application name.

arguments:
  -h, --help    show this help message and exit

arguments:
  <command>
    list        List secret names
    set         Set a secret; prompts for the value when neither --value nor --value-stdin is given
    import      Import every variable of a .env file into an application scope
    remove      Remove a secret
```

## `luma secret list`

```text
usage: luma secret list [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

List secret names

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma secret set`

```text
usage: luma secret set [-h] [--scope SCOPE] [--value VALUE] [--value-stdin] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] name

Set a secret; prompts for the value when neither --value nor --value-stdin is given

arguments:
  name                        Variable name, for example DATABASE_URL

arguments:
  -h, --help                  show this help message and exit
  --scope SCOPE               Application name the secret belongs to
  --value VALUE               Secret value (visible in shell history; prefer the prompt or --value-
                              stdin)
  --value-stdin               Read the value from stdin

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma secret import`

```text
usage: luma secret import [-h] --scope SCOPE [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] ENV_FILE

Import every variable of a .env file into an application scope

arguments:
  ENV_FILE                    .env file to import

arguments:
  -h, --help                  show this help message and exit
  --scope SCOPE               Application name the secrets belong to

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma secret remove`

```text
usage: luma secret remove [-h] [--scope SCOPE] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] name

Remove a secret

arguments:
  name                        Variable name

arguments:
  -h, --help                  show this help message and exit
  --scope SCOPE               Application name the secret belongs to

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma bootstrap`

```text
usage: luma bootstrap [-h] --domain DOMAIN [--node NODE] [--profile {cn-edge,egress-gateway,global-worker,home-node,single-node}] [--http-port HTTP_PORT] [--https-port HTTPS_PORT] [--skip-egress] [--overwrite-control-state]

Install the Luma manager on this Linux server: Docker, Nomad, Traefik, Luma Control and, when
EGRESS_SUBSCRIPTION_URL is set, the egress proxy. Prompts for missing settings. Safe to rerun to
repair a layer.

arguments:
  -h, --help                  show this help message and exit
  --domain DOMAIN             Hostname for the Control API and dashboard
  --node NODE                 Manager node name from the cluster config (default: this host)
  --profile {cn-edge,egress-gateway,global-worker,home-node,single-node}
                              Roles installed on this host (default: single-node)
  --http-port HTTP_PORT       Public HTTP port for Traefik (default: 80)
  --https-port HTTPS_PORT     Public HTTPS port for Traefik (default: 443)
  --skip-egress               Do not install the egress proxy
  --overwrite-control-state   Create new Control state and tokens instead of reusing the existing
                              ones

Example: luma bootstrap --domain luma.example.com
```

## `luma update`

```text
usage: luma update [-h] [--install-ref INSTALL_REF] [--domain DOMAIN] [--detach] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [target] ...

Update the local CLI. On a manager this also refreshes Luma Control, and on a joined node it
refreshes the node agent.

arguments:
  -h, --help                  show this help message and exit
  --install-ref INSTALL_REF   Git tag, branch or commit to install (default: the latest release)
  --domain DOMAIN             Control domain, when it changed (default: the domain in Control state)
  --detach                    Run a manager update in the background and log progress locally

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host

arguments:
  [target]
    manager                   Force a manager control-plane refresh
    fleet                     Update Luma on every registered node with a ready agent

Examples:
  luma update
  luma update --install-ref v0.2.0
  luma update fleet
```

## `luma update manager`

```text
usage: luma update manager [-h] [--install-ref INSTALL_REF] [--domain DOMAIN] [--detach] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP]

Force a manager control-plane refresh

arguments:
  -h, --help                  show this help message and exit
  --install-ref INSTALL_REF   Git tag, branch or commit to install (default: the latest release)
  --domain DOMAIN             Control domain, when it changed (default: the domain in Control state)
  --detach                    Run a manager update in the background and log progress locally

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma update fleet`

```text
usage: luma update fleet [-h] [--install-ref FLEET_INSTALL_REF] [--all] [--include-manager] [--timeout TIMEOUT] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

Update Luma on every registered node with a ready agent

arguments:
  -h, --help                  show this help message and exit
  --install-ref FLEET_INSTALL_REF
                              Git ref installed on every node
  --all                       Also list offline nodes as skipped
  --include-manager           Also update manager nodes
  --timeout TIMEOUT           Per-node timeout in seconds (default: 900)
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma node`

```text
usage: luma node [-h] <command> ...

Join, list and remove nodes

arguments:
  -h, --help    show this help message and exit

arguments:
  <command>
    join        Join this machine to the cluster
    list        List registered nodes
    status      Show node readiness, agent and resource details
    remove      Unregister a node from Control
    exit        Stop Nomad on this machine and remove its Luma state
    tailscale   Install Tailscale on this machine and join the tailnet (needs TAILSCALE_AUTHKEY)
    nomad-join  Ask a ready node's agent to reinstall Nomad and rejoin
```

## `luma node join`

```text
usage: luma node join [-h] --token TOKEN [--region REGION] [--name NAME] [--insecure] [--resolve-ip RESOLVE_IP] endpoint

Join this machine to the cluster

arguments:
  endpoint                 Control URL

arguments:
  -h, --help               show this help message and exit
  --token TOKEN            Node join token printed by 'luma bootstrap'
  --region REGION          cn, global, home or a region from 'luma region create'
  --name NAME              Node name used by manifests' node field (default: hostname)
  --insecure               Skip TLS verification for self-signed endpoints
  --resolve-ip RESOLVE_IP  Connect to this IP while keeping the endpoint hostname as Host

Example: luma node join https://luma.example.com --token <node-join-token> --region global --name
sg-1
```

## `luma node list`

```text
usage: luma node list [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

List registered nodes

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma node status`

```text
usage: luma node status [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] [name]

Show node readiness, agent and resource details

arguments:
  name                        Only this node (name, hostname or alias)

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma node remove`

```text
usage: luma node remove [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] name

Unregister a node from Control

arguments:
  name                        Node name

arguments:
  -h, --help                  show this help message and exit

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma node exit`

```text
usage: luma node exit [-h] [--endpoint ENDPOINT] [--token TOKEN] [--name NAME] [--tailscale] [--prune-docker] [--insecure] [--resolve-ip RESOLVE_IP]

Stop Nomad on this machine and remove its Luma state

arguments:
  -h, --help               show this help message and exit
  --endpoint ENDPOINT      Control URL; also unregister this node
  --token TOKEN            Management or node join token used with --endpoint
  --name NAME              Node name to unregister (default: this node's registered name)
  --tailscale              Also log out of Tailscale
  --prune-docker           Also prune unused Docker containers, networks, images and volumes
  --insecure               Skip TLS verification for self-signed endpoints
  --resolve-ip RESOLVE_IP  Connect to this IP while keeping the endpoint hostname as Host
```

## `luma node tailscale`

```text
usage: luma node tailscale [-h]

Install Tailscale on this machine and join the tailnet (needs TAILSCALE_AUTHKEY)

arguments:
  -h, --help  show this help message and exit
```

## `luma node nomad-join`

```text
usage: luma node nomad-join [-h] [--region REGION] [--server-addr SERVER_ADDR] [--timeout TIMEOUT] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] name

Ask a ready node's agent to reinstall Nomad and rejoin

arguments:
  name                        Node name

arguments:
  -h, --help                  show this help message and exit
  --region REGION             Override the node's registered region
  --server-addr SERVER_ADDR   Nomad RPC address (default: the manager's join address)
  --timeout TIMEOUT           Seconds to wait (default: 1200)
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma region`

```text
usage: luma region [-h] <command> ...

Manage scheduling regions

arguments:
  -h, --help    show this help message and exit

arguments:
  <command>
    list        List built-in and custom regions
    create      Create a custom region
    remove      Remove an unused custom region
```

## `luma region list`

```text
usage: luma region list [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

List built-in and custom regions

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma region create`

```text
usage: luma region create [-h] [--egress {proxy,direct}] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] name

Create a custom region

arguments:
  name                        Region name

arguments:
  -h, --help                  show this help message and exit
  --egress {proxy,direct}     Joins and image pulls use the manager proxy or go direct (default:
                              proxy)
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma region remove`

```text
usage: luma region remove [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] name

Remove an unused custom region

arguments:
  name                        Region name

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma storage`

```text
usage: luma storage [-h] <command> ...

Manage NFS storage classes

arguments:
  -h, --help    show this help message and exit

arguments:
  <command>
    list        List storage classes
    set         Create or update an NFS storage class
    remove      Remove a storage class
    apply       Prepare the volumes a Compose sidecar needs
    check       Check that a sidecar's volumes can be mounted where it runs
    migrate     Print a manual plan for moving a volume's data
```

## `luma storage list`

```text
usage: luma storage list [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

List storage classes

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma storage set`

```text
usage: luma storage set [-h] [--provider {nfs}] [--node NODE] [--path PATH] [--external] [--endpoint ENDPOINT] [--mount-options MOUNT_OPTIONS] [--region REGIONS] [--eligible-node NODES] [--timeout TIMEOUT] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] name

Create or update an NFS storage class

arguments:
  name                        Storage class name

arguments:
  -h, --help                  show this help message and exit
  --provider {nfs}            Storage provider (default: nfs)
  --node NODE                 Node that serves a managed NFS export
  --path PATH                 Export path on the NFS server
  --external                  Use an existing NFS server instead of preparing one
  --endpoint ENDPOINT         Address of an external NFS server
  --mount-options MOUNT_OPTIONS
                              NFS mount options (default:
                              nfsvers=4,rw,soft,timeo=100,retrans=10,noresvport)
  --region REGIONS            Region allowed to mount it; repeat for several
  --eligible-node NODES       Node allowed to mount it; repeat for several
  --timeout TIMEOUT           Seconds to wait for host preparation (default: 360)

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma storage remove`

```text
usage: luma storage remove [-h] [--timeout TIMEOUT] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] name

Remove a storage class

arguments:
  name                        Storage class name

arguments:
  -h, --help                  show this help message and exit
  --timeout TIMEOUT           Seconds to wait for host cleanup (default: 360)

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma storage apply`

```text
usage: luma storage apply [-h] [--dry-run] [--timeout TIMEOUT] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] SIDECAR

Prepare the volumes a Compose sidecar needs

arguments:
  SIDECAR                     Luma Compose sidecar

arguments:
  -h, --help                  show this help message and exit
  --dry-run                   Show the plan without preparing anything
  --timeout TIMEOUT           Seconds to wait (default: 300)

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma storage check`

```text
usage: luma storage check [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] SIDECAR

Check that a sidecar's volumes can be mounted where it runs

arguments:
  SIDECAR                     Luma Compose sidecar

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma storage migrate`

```text
usage: luma storage migrate [-h] --volume VOLUME --from-node FROM_NODE --from-volume FROM_VOLUME [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] SIDECAR

Print a manual plan for moving a volume's data

arguments:
  SIDECAR                     Luma Compose sidecar

arguments:
  -h, --help                  show this help message and exit
  --volume VOLUME             Volume in the sidecar
  --from-node FROM_NODE       Node that holds the current data
  --from-volume FROM_VOLUME   Current Docker volume or path
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma registry`

```text
usage: luma registry [-h] <command> ...

Manage registry credentials and the managed registry

arguments:
  -h, --help    show this help message and exit

arguments:
  <command>
    list        List saved registry credentials
    login       Save credentials for pulling private images
    remove      Remove saved registry credentials
    serve       Deploy the managed registry on a Linux node
    images      List images in the managed registry and their protection state
    delete      Queue a manifest for deletion after a protection check
    deletion    Cancel, execute or restore a queued deletion
    gc          Preview or run registry garbage collection (irreversible)
    policy      Show or change the retention policy
```

## `luma registry list`

```text
usage: luma registry list [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

List saved registry credentials

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma registry login`

```text
usage: luma registry login [-h] --username USERNAME [--password-stdin] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] host

Save credentials for pulling private images

arguments:
  host                        Registry host, for example ghcr.io

arguments:
  -h, --help                  show this help message and exit
  --username USERNAME         Registry user name
  --password-stdin            Read the password or token from stdin (otherwise prompt)

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma registry remove`

```text
usage: luma registry remove [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] host

Remove saved registry credentials

arguments:
  host                        Registry host

arguments:
  -h, --help                  show this help message and exit

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma registry serve`

```text
usage: luma registry serve [-h] --node NODE [--port PORT] [--domain DOMAIN] [--username USERNAME] [--password-stdin] [--storage-class STORAGE_CLASS] [--image IMAGE] [--name NAME] [--no-activate] [--timeout TIMEOUT] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

Deploy the managed registry on a Linux node

arguments:
  -h, --help                  show this help message and exit
  --node NODE                 Ready Linux node that hosts the registry
  --port PORT                 Host port (default: 5000)
  --domain DOMAIN             TLS hostname; avoids reconfiguring Docker on every node
  --username USERNAME         Basic Auth user for --domain
  --password-stdin            Read the --domain password from stdin
  --storage-class STORAGE_CLASS
                              Storage class for images (default: a node-local volume)
  --image IMAGE               Registry image (default: registry:2)
  --name NAME                 Service name (default: luma-registry)
  --no-activate               Do not make it the builders' push and pull registry
  --timeout TIMEOUT           Seconds to wait (default: 1800)
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma registry images`

```text
usage: luma registry images [-h] [--refresh] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

List images in the managed registry and their protection state

arguments:
  -h, --help                  show this help message and exit
  --refresh                   Rescan the registry instead of using the cache
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma registry delete`

```text
usage: luma registry delete [-h] [--execute-now] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] repository digest

Queue a manifest for deletion after a protection check

arguments:
  repository                  Repository, for example acme/web
  digest                      Manifest digest (sha256:...)

arguments:
  -h, --help                  show this help message and exit
  --execute-now               Skip the grace period after a fresh protection check
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma registry deletion`

```text
usage: luma registry deletion [-h] [--force] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] id {cancel,execute,restore}

Cancel, execute or restore a queued deletion

arguments:
  id                          Deletion ID
  {cancel,execute,restore}    What to do

arguments:
  -h, --help                  show this help message and exit
  --force                     Proceed despite warnings
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma registry gc`

```text
usage: luma registry gc [-h] [--execute] [--force] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

Preview or run registry garbage collection (irreversible)

arguments:
  -h, --help                  show this help message and exit
  --execute                   Run it instead of previewing
  --force                     Proceed despite warnings
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma registry policy`

```text
usage: luma registry policy [-h] [--mode {off,recommend,enforce}] [--keep-last KEEP_LAST] [--max-age-days MAX_AGE_DAYS] [--system-keep-last SYSTEM_KEEP_LAST] [--queue-grace-hours QUEUE_GRACE_HOURS] [--gc-grace-days GC_GRACE_DAYS] [--warning-percent WARNING_PERCENT] [--critical-percent CRITICAL_PERCENT] [--emergency-percent EMERGENCY_PERCENT] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

Show or change the retention policy

arguments:
  -h, --help                  show this help message and exit
  --mode {off,recommend,enforce}
                              off, recommend deletions, or enforce them
  --keep-last KEEP_LAST       Tags to keep per repository
  --max-age-days MAX_AGE_DAYS
                              Delete older unprotected images
  --system-keep-last SYSTEM_KEEP_LAST
                              Tags to keep for Luma system images
  --queue-grace-hours QUEUE_GRACE_HOURS
                              Hours before queued deletions run
  --gc-grace-days GC_GRACE_DAYS
                              Days before garbage collection runs
  --warning-percent WARNING_PERCENT
                              Disk usage that raises a warning
  --critical-percent CRITICAL_PERCENT
                              Disk usage that raises a critical alert
  --emergency-percent EMERGENCY_PERCENT
                              Disk usage that triggers emergency cleanup
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma git-provider`

```text
usage: luma git-provider [-h] <command> ...

Manage saved GitHub and Gitea credentials

arguments:
  -h, --help    show this help message and exit

arguments:
  <command>
    list        List saved provider credentials
    set         Save a GitHub or Gitea access token
    remove      Remove a saved credential
    repos       List repositories a credential can read
    refs        List branches and tags of a repository
```

## `luma git-provider list`

```text
usage: luma git-provider list [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet]

List saved provider credentials

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma git-provider set`

```text
usage: luma git-provider set [-h] [--token-stdin] [--git-token PROVIDER_TOKEN] [--username USERNAME] [--base-url BASE_URL] [--clone-base-url CLONE_BASE_URL] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] {github,gitea} account

Save a GitHub or Gitea access token

arguments:
  {github,gitea}              Provider type
  account                     Label, for example personal or work

arguments:
  -h, --help                  show this help message and exit
  --token-stdin               Read the token from stdin
  --git-token PROVIDER_TOKEN  Token (visible in shell history; prefer --token-stdin)
  --username USERNAME         Git user name for HTTPS clones
  --base-url BASE_URL         API base URL; required for Gitea
  --clone-base-url CLONE_BASE_URL
                              Clone URL base (default: derived from the base URL)

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma git-provider remove`

```text
usage: luma git-provider remove [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] id

Remove a saved credential

arguments:
  id                          Credential ID, for example github:personal

arguments:
  -h, --help                  show this help message and exit

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma git-provider repos`

```text
usage: luma git-provider repos [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] id

List repositories a credential can read

arguments:
  id                          Credential ID

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma git-provider refs`

```text
usage: luma git-provider refs [-h] [--control-context CONTROL_CONTEXT] [--control-url CONTROL_URL] [--token TOKEN] [--insecure] [--resolve-ip RESOLVE_IP] [--format {text,json,ndjson}] [--quiet] id repository

List branches and tags of a repository

arguments:
  id                          Credential ID
  repository                  Repository full name, for example owner/name

arguments:
  -h, --help                  show this help message and exit
  --format {text,json,ndjson}
                              Output format (default: text)
  --quiet                     Only print the final result or error

arguments:
  --control-context CONTROL_CONTEXT
                              Use this saved login without switching to it
  --control-url CONTROL_URL   Control URL to use instead of the current login
  --token TOKEN               Management token for --control-url (or set LUMA_DEPLOY_TOKEN)
  --insecure                  Skip TLS verification for Control
  --resolve-ip RESOLVE_IP     Connect to this IP while keeping the Control hostname as Host
```

## `luma manager`

```text
usage: luma manager [-h] <command> ...

Repair the manager. Run these on the manager itself.

arguments:
  -h, --help    show this help message and exit

arguments:
  <command>
    egress      Install or refresh the egress proxy (needs EGRESS_SUBSCRIPTION_URL)
    cloudflare  Point the cluster config at a Cloudflare zone
    ip-change   Recover Control after the manager's public IPv4 address changes
```

## `luma manager egress`

```text
usage: luma manager egress [-h]

Install or refresh the egress proxy (needs EGRESS_SUBSCRIPTION_URL)

arguments:
  -h, --help  show this help message and exit
```

## `luma manager cloudflare`

```text
usage: luma manager cloudflare [-h] --zone ZONE

Point the cluster config at a Cloudflare zone

arguments:
  -h, --help   show this help message and exit
  --zone ZONE  Zone name, for example example.com
```

## `luma manager ip-change`

```text
usage: luma manager ip-change [-h] --old OLD_IP --new NEW_IP --domain DOMAIN [--dry-run]

Recover Control after the manager's public IPv4 address changes

arguments:
  -h, --help       show this help message and exit
  --old OLD_IP     Previous public IPv4 address
  --new NEW_IP     New public IPv4 address
  --domain DOMAIN  Control hostname, without scheme
  --dry-run        Show the recovery plan without changing anything
```
