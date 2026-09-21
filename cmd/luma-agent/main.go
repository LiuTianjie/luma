package main

import (
	"context"
	"github.com/LiuTianjie/luma/internal/adapters/process"
	"github.com/LiuTianjie/luma/internal/agentapp"
	"os"
	"os/signal"
	"syscall"
)

var version = "dev"

func main() {
	if len(os.Args) > 1 && os.Args[1] == process.SupervisorArgument {
		os.Exit(process.Supervise(os.Args[2:]))
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := agentapp.Run(ctx, os.Args[1:], os.Stdout, os.Stderr, version)
	stop()
	os.Exit(code)
}
