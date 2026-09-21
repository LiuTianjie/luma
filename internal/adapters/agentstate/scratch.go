package agentstate

import (
	"fmt"
	"os"
)

// Scratch is called only while holding this config's exclusive agent lock. It
// removes temporary credential files left by a killed process before any task
// is leased. It never follows a substituted scratch-root symlink.
func Scratch(configPath string) (string, error) {
	path := configPath + ".go-tmp"
	if err := os.Mkdir(path, 0700); err != nil && !os.IsExist(err) {
		return "", err
	}
	info, err := os.Lstat(path)
	if err != nil {
		return "", err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0077 != 0 {
		return "", fmt.Errorf("agent scratch must be a private directory")
	}
	root, err := os.OpenRoot(path)
	if err != nil {
		return "", err
	}
	defer root.Close()
	dir, err := root.Open(".")
	if err != nil {
		return "", err
	}
	defer dir.Close()
	entries, err := dir.ReadDir(-1)
	if err != nil {
		return "", err
	}
	for _, entry := range entries {
		if err := root.RemoveAll(entry.Name()); err != nil {
			return "", err
		}
	}
	return path, nil
}
