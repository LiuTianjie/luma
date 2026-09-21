//go:build linux

package buildexec

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

var rootlessEndpoint = regexp.MustCompile(`^unix:///run/user/([1-9][0-9]*)/docker\.sock$`)

func rootlessPeer(ctx context.Context, endpoint string) (analysisPeer, error) {
	match := rootlessEndpoint.FindStringSubmatch(endpoint)
	if match == nil {
		return analysisPeer{}, fmt.Errorf("Docker endpoint must be unix:///run/user/<uid>/docker.sock")
	}
	expected, err := strconv.Atoi(match[1])
	if err != nil || expected <= 0 {
		return analysisPeer{}, fmt.Errorf("invalid rootless Docker UID")
	}
	socketPath := filepath.FromSlash(endpoint[len("unix://"):])
	socketInfo, err := os.Lstat(socketPath)
	if err != nil {
		return analysisPeer{}, fmt.Errorf("rootless Docker socket is unavailable")
	}
	runtimeInfo, err := os.Lstat(filepath.Dir(socketPath))
	if err != nil {
		return analysisPeer{}, fmt.Errorf("rootless Docker runtime directory is unavailable")
	}
	statSocket, ok := socketInfo.Sys().(*syscall.Stat_t)
	if !ok || socketInfo.Mode()&os.ModeSocket == 0 || statSocket.Uid == 0 || int(statSocket.Uid) != expected {
		return analysisPeer{}, fmt.Errorf("Docker socket is not owned by its non-root runtime UID")
	}
	statRuntime, ok := runtimeInfo.Sys().(*syscall.Stat_t)
	if !ok || !runtimeInfo.IsDir() || runtimeInfo.Mode()&os.ModeSymlink != 0 || int(statRuntime.Uid) != expected || int(statRuntime.Gid) != int(statSocket.Gid) {
		return analysisPeer{}, fmt.Errorf("Docker rootless runtime ownership is invalid")
	}
	dialer := net.Dialer{Timeout: 2 * time.Second}
	conn, err := dialer.DialContext(ctx, "unix", socketPath)
	if err != nil {
		return analysisPeer{}, fmt.Errorf("Docker daemon peer credentials could not be verified")
	}
	defer conn.Close()
	unixConn, ok := conn.(*net.UnixConn)
	if !ok {
		return analysisPeer{}, fmt.Errorf("Docker endpoint is not a Unix socket")
	}
	var peer unix.Ucred
	err = unixConn.SyscallConn().Control(func(fd uintptr) { peer, err = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED) })
	if err != nil || peer.Pid <= 0 || peer.Uid == 0 || int(peer.Uid) != expected || int(peer.Uid) != int(statSocket.Uid) || int(peer.Gid) != int(statSocket.Gid) {
		return analysisPeer{}, fmt.Errorf("Docker daemon peer credentials do not match socket owner")
	}
	return analysisPeer{UID: int(peer.Uid), GID: int(peer.Gid)}, nil
}
