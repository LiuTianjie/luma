//go:build linux || darwin

package hostadmin

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

type fileState struct {
	data     []byte
	exists   bool
	mode     os.FileMode
	uid, gid int
}
type hostFiles struct{ root *os.Root }

func (f hostFiles) read(path string) (fileState, error) {
	file, err := f.root.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if os.IsNotExist(err) {
		return fileState{}, nil
	}
	if err != nil {
		return fileState{}, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return fileState{}, err
	}
	if !info.Mode().IsRegular() {
		return fileState{}, fmt.Errorf("host configuration is not a regular file")
	}
	data, err := io.ReadAll(io.LimitReader(file, 1<<20+1))
	if err != nil {
		return fileState{}, err
	}
	if len(data) > 1<<20 {
		return fileState{}, fmt.Errorf("host configuration exceeds limit")
	}
	s := fileState{data: data, exists: true, mode: info.Mode().Perm(), uid: -1, gid: -1}
	if stat, ok := info.Sys().(*syscall.Stat_t); ok {
		s.uid, s.gid = int(stat.Uid), int(stat.Gid)
	}
	return s, nil
}
func (f hostFiles) write(path string, state fileState) error {
	if !state.exists {
		err := f.root.Remove(path)
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		return f.sync(filepath.Dir(path))
	}
	if err := f.root.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return err
	}
	temp := path + fmt.Sprintf(".luma-%x", randomBytes())
	file, err := f.root.OpenFile(temp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	defer f.root.Remove(temp)
	defer file.Close()
	if _, err = file.Write(state.data); err != nil {
		return err
	}
	if state.uid >= 0 {
		if err = file.Chown(state.uid, state.gid); err != nil {
			return err
		}
	}
	if err = file.Chmod(state.mode); err != nil {
		return err
	}
	if err = file.Sync(); err != nil {
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	if err = f.root.Rename(temp, path); err != nil {
		return err
	}
	return f.sync(filepath.Dir(path))
}
func randomBytes() []byte {
	b := make([]byte, 12)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return b
}
func (f hostFiles) sync(path string) error {
	dir, err := f.root.Open(path)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

// apply records unfinished activation separately from config bytes. Retrying an
// identical file after a crash must still reload/restart the relevant service.
// Errors restore the original file and attempt service recovery with a bounded,
// uncanceled context. An unsuccessful recovery retains the activation marker.
func (f hostFiles) apply(ctx context.Context, path string, old, desired fileState, force bool, activate func(context.Context, bool) error) (bool, error) {
	current, err := f.read(path)
	if err != nil {
		return false, err
	}
	if current.exists != old.exists || !bytes.Equal(current.data, old.data) || current.mode != old.mode {
		return false, fmt.Errorf("host configuration changed during preparation; retry with its new contents")
	}
	marker := path + ".luma-go-apply"
	pending, err := f.read(marker)
	if err != nil {
		return false, err
	}
	changed := old.exists != desired.exists || !bytes.Equal(old.data, desired.data) || (desired.exists && old.mode != desired.mode)
	if !changed && !pending.exists && !force {
		return false, nil
	}
	if err := ctx.Err(); err != nil {
		return false, err
	}
	hash := sha256.Sum256(desired.data)
	if err := f.write(marker, fileState{exists: true, data: []byte(fmt.Sprintf("%x\n", hash)), mode: 0600, uid: -1, gid: -1}); err != nil {
		return false, err
	}
	if changed {
		if err := f.write(path, desired); err != nil {
			return false, fmt.Errorf("write host configuration: %w", err)
		}
	}
	if err := activate(ctx, true); err != nil {
		recovery, stop := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
		defer stop()
		current, readErr := f.read(path)
		if readErr != nil || current.exists != desired.exists || !bytes.Equal(current.data, desired.data) || (desired.exists && current.mode != desired.mode) {
			return false, fmt.Errorf("host activation failed and configuration changed during activation; preserving the newer file for manual recovery")
		}
		if rollback := f.write(path, old); rollback != nil {
			return false, fmt.Errorf("host activation failed; restoring configuration also failed")
		}
		if rollback := activate(recovery, false); rollback != nil {
			return false, fmt.Errorf("host activation failed; configuration restored but service recovery failed")
		}
		if cleanup := f.write(marker, fileState{}); cleanup != nil {
			return false, fmt.Errorf("host activation failed; restored configuration but recovery marker could not be cleared")
		}
		return false, fmt.Errorf("host activation failed; original configuration restored: %w", err)
	}
	if err := f.write(marker, fileState{}); err != nil {
		return true, fmt.Errorf("host configuration activated but activation marker could not be cleared: %w", err)
	}
	return true, nil
}
