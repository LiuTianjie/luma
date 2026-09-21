//go:build linux || darwin

package buildexec

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"syscall"
)

func prepareAnalysisBinds(p *PreparedAnalysis, peer analysisPeer, maxEntries int) error {
	if peer.UID <= 0 || peer.UID != os.Geteuid() {
		return fmt.Errorf("rootless Docker daemon must run as the Agent user")
	}
	if p == nil || p.Work == "" || p.Workspace == "" || p.Input == "" || p.Output == "" {
		return fmt.Errorf("analysis workspace paths are incomplete")
	}
	for _, root := range []string{p.Work, p.Workspace, p.Input, p.Output} {
		if err := requireOwnedDirectory(root, peer, maxEntries); err != nil {
			return err
		}
	}
	if err := emptyDirectory(p.Output); err != nil {
		return err
	}
	if err := chmodTree(p.Workspace, 0500, 0400, true, maxEntries); err != nil {
		return err
	}
	if err := chmodTree(p.Input, 0500, 0400, false, maxEntries); err != nil {
		return err
	}
	if err := os.Chmod(p.Work, 0700); err != nil {
		return err
	}
	return os.Chmod(p.Output, 0700)
}

func hardenAnalysisOutput(root string, peer analysisPeer, maxEntries int) error {
	if err := requireOwnedDirectory(root, peer, maxEntries); err != nil {
		return err
	}
	return chmodTree(root, 0700, 0600, false, maxEntries)
}

func requireOwnedDirectory(root string, peer analysisPeer, maxEntries int) error {
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("analysis bind must be a real directory")
	}
	if !ownedBy(info, peer) {
		return fmt.Errorf("analysis bind ownership does not match rootless Docker peer")
	}
	count := 0
	return filepath.WalkDir(root, func(name string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		count++
		if count > maxEntries*2+16 {
			return fmt.Errorf("analysis bind has too many entries")
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return nil
		}
		if !ownedBy(info, peer) {
			return fmt.Errorf("analysis bind entry ownership does not match rootless Docker peer")
		}
		if !info.IsDir() && !info.Mode().IsRegular() {
			return fmt.Errorf("analysis bind contains a special file")
		}
		return nil
	})
}

func chmodTree(root string, dirMode, fileMode os.FileMode, allowSymlink bool, maxEntries int) error {
	count := 0
	return filepath.WalkDir(root, func(name string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		count++
		if count > maxEntries*2+16 {
			return fmt.Errorf("analysis output has too many entries")
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			if allowSymlink {
				return nil
			}
			return fmt.Errorf("analysis output contains a symlink")
		}
		if info.IsDir() {
			return os.Chmod(name, dirMode)
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("analysis output contains a special file")
		}
		return os.Chmod(name, fileMode)
	})
}

func emptyDirectory(root string) error {
	entries, err := os.ReadDir(root)
	if err != nil {
		return err
	}
	if len(entries) > 0 {
		return fmt.Errorf("analysis output bind must be empty")
	}
	return nil
}

func ownedBy(info os.FileInfo, peer analysisPeer) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && int(stat.Uid) == peer.UID && int(stat.Gid) == peer.GID
}
