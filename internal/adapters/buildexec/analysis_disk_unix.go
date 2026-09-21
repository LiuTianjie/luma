//go:build linux || darwin

package buildexec

import (
	"fmt"
	"syscall"
)

func analysisFreeDisk(path string, required int64) error {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return err
	}
	if uint64(stat.Bavail)*uint64(stat.Bsize) < uint64(required) {
		return fmt.Errorf("insufficient free space for analysis disk budget")
	}
	return nil
}
