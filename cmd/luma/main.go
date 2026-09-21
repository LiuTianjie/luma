package main

import (
	"context"
	"github.com/LiuTianjie/luma/internal/cli"
	"os"
	"os/signal"
	"syscall"
)

// Injected by scripts/build-go.sh from the repository's existing version source.
var version = "dev"

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := cli.Run(ctx, os.Args[1:], cli.IO{In: os.Stdin, Out: os.Stdout, Err: os.Stderr, Getenv: os.Getenv, LookupEnv: os.LookupEnv}, version)
	stop()
	os.Exit(code)
}
