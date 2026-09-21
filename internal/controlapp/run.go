// Package controlapp is the Control composition root. It connects repositories
// to HTTP adapters and owns startup/shutdown, never deployment policy.
package controlapp

import (
	"context"
	"crypto/tls"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"time"

	"github.com/LiuTianjie/luma/internal/adapters/cloudflare"
	"github.com/LiuTianjie/luma/internal/adapters/controlserver"
	"github.com/LiuTianjie/luma/internal/adapters/controlstore"
	"github.com/LiuTianjie/luma/internal/adapters/deploymentfiles"
	"github.com/LiuTianjie/luma/internal/adapters/manifest"
	"github.com/LiuTianjie/luma/internal/adapters/nomad"
	"github.com/LiuTianjie/luma/internal/adapters/nomadruntime"
	"github.com/LiuTianjie/luma/internal/adapters/registryhttp"
	"github.com/LiuTianjie/luma/internal/adapters/routeprobe"
	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/history"
	"github.com/LiuTianjie/luma/internal/node"
	"github.com/LiuTianjie/luma/internal/workflow"
)

func Run(ctx context.Context, args []string, out, errout io.Writer, version string) int {
	flags := flag.NewFlagSet("luma-control", flag.ContinueOnError)
	flags.SetOutput(errout)
	dir := flags.String("state-dir", "", "restored private Control state directory (required)")
	address := flags.String("listen", "127.0.0.1:9081", "loopback listen address for migration verification")
	readOnly := flags.Bool("read-only", false, "run the read-only migration service")
	agentTasks := flags.Bool("agent-tasks", false, "enable migrated Agent task and workflow transactions on restored state")
	cert := flags.String("tls-cert", "", "TLS certificate PEM (required for Agent task mode)")
	key := flags.String("tls-key", "", "TLS private key PEM (required for Agent task mode)")
	showVersion := flags.Bool("version", false, "show Control version")
	routeCAFile := flags.String("route-ca-file", "", "optional trusted private CA PEM for public route probes")
	configPath := flags.String("config", os.Getenv("LUMA_CONTROL_CONFIG"), "Control YAML configuration; required for public deployment integrations")
	nomadURL := flags.String("nomad-url", "", "explicit isolated Nomad endpoint; enables internal deployment migration lane")
	rolloutTimeout := flags.Duration("rollout-timeout", 10*time.Minute, "maximum Nomad rollout wait")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 1
	}
	if flags.NArg() != 0 {
		fmt.Fprintln(errout, "unexpected Control arguments")
		return 1
	}
	if *showVersion {
		fmt.Fprintln(out, version)
		return 0
	}
	if *dir == "" || *readOnly == *agentTasks {
		fmt.Fprintln(errout, "--state-dir RESTORED_DIRECTORY and exactly one of --read-only or --agent-tasks are required; Go Control is not ready for production cutover")
		return 1
	}
	if *nomadURL != "" && !*agentTasks || *rolloutTimeout <= 0 || *rolloutTimeout > time.Hour {
		fmt.Fprintln(errout, "--nomad-url requires --agent-tasks; rollout timeout must be positive and at most 1h")
		return 1
	}
	if (*cert == "") != (*key == "") || *agentTasks && *cert == "" {
		fmt.Fprintln(errout, "--tls-cert and --tls-key are required together; Agent task mode requires TLS")
		return 1
	}
	host, _, err := net.SplitHostPort(*address)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		fmt.Fprintln(errout, "migration Control must listen on an explicit loopback IP")
		return 1
	}
	if err := run(ctx, *dir, *address, version, *agentTasks, *cert, *key, *nomadURL, *rolloutTimeout, *configPath, *routeCAFile, out); err != nil && !errors.Is(err, context.Canceled) {
		fmt.Fprintln(errout, "luma-control:", err)
		return 1
	}
	return 0
}
func run(ctx context.Context, dir, address, version string, agentTasks bool, cert, key, nomadURL string, rolloutTimeout time.Duration, configPath, routeCAFile string, out io.Writer) error {
	var certificates []tls.Certificate
	if cert != "" {
		certificate, err := tls.LoadX509KeyPair(cert, key)
		if err != nil {
			return err
		}
		certificates = []tls.Certificate{certificate}
	}
	open := controlstore.OpenReadOnly
	if agentTasks {
		open = controlstore.OpenWriter
	}
	store, err := open(ctx, dir)
	if err != nil {
		return err
	}
	defer store.Close()
	config, dnsConfig, configBase, err := loadConfig(configPath)
	if err != nil {
		return err
	}
	var runtime *nomadruntime.Client
	if nomadURL != "" {
		runtime, err = nomadruntime.New(nomadURL, "")
		if err != nil {
			return err
		}
		defer runtime.Close()
		if err := store.RecoverBuilds(ctx); err != nil {
			return err
		}
		if err := store.RecoverDeployments(ctx); err != nil {
			return err
		}
	}
	listener, err := net.Listen("tcp", address)
	if err != nil {
		return err
	}
	handler := controlserver.Server{Identity: store, History: history.Service{Repository: store}, Version: version}
	handler.Workflows = &workflow.Service{Repository: store}
	handler.WorkflowWrites = agentTasks
	handler.Secrets = store
	handler.SecretWrites = agentTasks
	handler.Registries = store
	handler.RegistryWrites = agentTasks
	handler.BuildConfig = store
	handler.BuildConfigWrites = agentTasks
	if runtime != nil {
		handler.Deployments = &deployment.Coordinator{Decoder: manifest.Decoder{}, Compiler: nomad.Compiler{}, ComposeCompiler: nomad.Compiler{}, Repository: store, Secrets: store, Runtime: runtime, Config: config, DNSConfig: dnsConfig, Timeout: rolloutTimeout}
		handler.Builds = &builddomain.Coordinator{Repository: store, Identity: store, Workflows: handler.Workflows, Planner: store, Decoder: manifest.Decoder{}, Deployer: handler.Deployments, Config: config}
		handler.Deployments.ImageCredentials = store
		handler.Deployments.NodeTasks = node.CommandTasks{Repository: store}
		handler.Deployments.Prerequisites = store
		handler.Deployments.ImageResolver = registryhttp.Resolver{}
		if value := os.Getenv("LUMA_LAE_BUILDER_REGISTRY_INSECURE"); value == "0" || value == "1" {
			insecure := value == "1"
			handler.Deployments.CacheInsecure = &insecure
		}
		if configBase != "" {
			files, err := deploymentfiles.New(config, configBase)
			if err != nil {
				listener.Close()
				return err
			}
			defer files.Close()
			dns, err := cloudflare.New(cloudflare.API, store, os.Getenv)
			if err != nil {
				listener.Close()
				return err
			}
			defer dns.Close()
			probe := routeprobe.New()
			defer probe.Close()
			if routeCAFile != "" {
				pem, err := os.ReadFile(routeCAFile)
				if err != nil {
					listener.Close()
					return err
				}
				if err := probe.TrustPEM(pem); err != nil {
					listener.Close()
					return err
				}
			}
			handler.Deployments.Files = files
			handler.Deployments.DNS = dns
			handler.Deployments.Prober = probe
		}
	}
	mode := "read-only"
	if agentTasks {
		handler.Agents = &node.Coordinator{Repository: store, Version: version}
		mode = "agent-tasks"
	}
	server := &http.Server{Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 40 * time.Second, WriteTimeout: 40 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16 << 10, BaseContext: func(net.Listener) context.Context { return ctx }}
	if len(certificates) > 0 {
		listener = tls.NewListener(listener, &tls.Config{MinVersion: tls.VersionTLS12, Certificates: certificates})
	}
	var buildDone chan error
	workerFailures := make(chan error, 1)
	if handler.Builds != nil {
		workerCtx, stopWorkers := context.WithCancel(ctx)
		buildDone = make(chan error, 1)
		go func() {
			err := handler.Builds.Serve(workerCtx, 4)
			if err != nil && workerCtx.Err() == nil {
				workerFailures <- err
			}
			buildDone <- err
		}()
		defer func() { stopWorkers(); <-buildDone }()
	}
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	fmt.Fprintf(out, "Luma Go Control %s listening on %s\n", mode, listener.Addr())
	select {
	case err := <-workerFailures:
		_ = server.Close()
		<-done
		return err
	case err := <-done:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		err := server.Shutdown(shutdown)
		if err != nil {
			_ = server.Close()
		}
		<-done
		return err
	}
}
