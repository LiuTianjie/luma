//go:build !linux

package buildexec

import (
	"context"
	"errors"
)

// The analyzer lane is Linux-only because its security proof depends on the
// Linux rootless Docker socket layout and SO_PEERCRED.
var rootlessEndpoint = unsupportedRootlessEndpoint{}

type unsupportedRootlessEndpoint struct{}

func (unsupportedRootlessEndpoint) MatchString(string) bool { return false }

var errUnsupportedRootless = errors.New("rootless analyzer runtime is supported only on Linux")

func rootlessPeer(context.Context, string) (analysisPeer, error) {
	return analysisPeer{}, errUnsupportedRootless
}
