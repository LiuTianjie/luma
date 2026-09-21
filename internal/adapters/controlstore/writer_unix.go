//go:build linux || darwin

package controlstore

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"syscall"
)

var ErrReadOnly = errors.New("Control repository is read-only")

// OpenWriter takes the exclusive lock shared with Python's SQLite connection
// adapter, then opens existing state only. Schema/bootstrap migration is separate.
func OpenWriter(ctx context.Context, directory string) (*Store, error) {
	absolute, err := filepath.Abs(directory)
	if err != nil {
		return nil, err
	}
	root, err := os.OpenRoot(absolute)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	lock, err := root.OpenFile("control-writer.lock", os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, err
	}
	info, err := lock.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		lock.Close()
		return nil, errors.New("Control writer lock must be a private regular file")
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		lock.Close()
		return nil, errors.New("Control state is in use by another Python or Go writer")
	}
	unlock := func() { _ = syscall.Flock(int(lock.Fd()), syscall.LOCK_UN); _ = lock.Close() }
	s, err := OpenReadOnly(ctx, absolute)
	if err != nil {
		unlock()
		return nil, err
	}
	s.unlock = unlock
	u := url.URL{Scheme: "file", Path: filepath.Join(absolute, databaseName)}
	u.RawQuery = url.Values{"mode": {"rw"}, "_txlock": {"immediate"}, "_pragma": {"busy_timeout(2000)", "foreign_keys(1)", "synchronous(FULL)", "trusted_schema(0)"}}.Encode()
	s.writer, err = sql.Open("sqlite", u.String())
	if err == nil {
		s.writer.SetMaxOpenConns(1)
		s.writer.SetMaxIdleConns(1)
		err = s.writer.PingContext(ctx)
	}
	if err != nil {
		s.Close()
		return nil, fmt.Errorf("open Control writer: %w", err)
	}
	return s, nil
}
