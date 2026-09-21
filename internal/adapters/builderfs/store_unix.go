//go:build linux || darwin

package builderfs

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

// The filename and flock protocol are shared with existing Builder cleanup.
func (s Store) acquire() (*os.Root, func(), error) {
	name := s.Root
	if name == "" {
		name = DefaultRoot
	}
	if !filepath.IsAbs(name) {
		return nil, nil, fmt.Errorf("builder store root must be absolute")
	}
	name = filepath.Clean(name)
	for p := name; ; p = filepath.Dir(p) {
		info, err := os.Lstat(p)
		if err != nil && !os.IsNotExist(err) {
			return nil, nil, err
		}
		if err == nil && !info.IsDir() {
			return nil, nil, fmt.Errorf("builder store root contains a symlink or non-directory")
		}
		if p == filepath.Dir(p) {
			break
		}
	}
	if err := os.MkdirAll(name, 0700); err != nil {
		return nil, nil, err
	}
	info, err := os.Lstat(name)
	if err != nil {
		return nil, nil, err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !info.IsDir() || !ok || stat.Uid != uint32(os.Geteuid()) || info.Mode().Perm()&0022 != 0 {
		return nil, nil, fmt.Errorf("builder store must be owned by this Agent and not writable by others")
	}
	lock, err := os.OpenFile(filepath.Join(filepath.Dir(name), ".luma-builder-storage-execution.lock"), os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, nil, err
	}
	lockInfo, err := lock.Stat()
	if err != nil || !lockInfo.Mode().IsRegular() || !ownedPrivate(lockInfo) {
		lock.Close()
		return nil, nil, fmt.Errorf("unsafe builder execution lock")
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		lock.Close()
		return nil, nil, fmt.Errorf("builder storage is busy with another build, export or cleanup operation")
	}
	release := func() { _ = syscall.Flock(int(lock.Fd()), syscall.LOCK_UN); _ = lock.Close() }
	root, err := os.OpenRoot(name)
	if err != nil {
		release()
		return nil, nil, err
	}
	return root, release, nil
}

func ownedPrivate(info os.FileInfo) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && stat.Uid == uint32(os.Geteuid()) && info.Mode().Perm()&0022 == 0
}

func privateParents(root *os.Root, name string) error {
	parts := strings.Split(name, "/")
	for i := range parts {
		info, err := root.Lstat(strings.Join(parts[:i+1], "/"))
		if err != nil {
			return err
		}
		if !info.IsDir() || !ownedPrivate(info) {
			return fmt.Errorf("unsafe builder content directory")
		}
	}
	return nil
}

func openNoFollow(root *os.Root, name string) (*os.File, error) {
	return root.OpenFile(name, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
}
