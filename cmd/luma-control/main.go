package main

import (
	"context"
	"os"
	"os/signal"
	"syscall"

	"github.com/LiuTianjie/luma/internal/controlapp"
)

var version = "dev"

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := controlapp.Run(ctx, os.Args[1:], os.Stdout, os.Stderr, version)
	stop()
	os.Exit(code)
}
