// Package storage owns node-local volume validation. Persistent storage belongs
// to the deployment's selected node; there is no remote storage-class backend.
package storage

import (
	"fmt"
	"path"
	"slices"
	"strings"
)

type Volume struct {
	Name, AccessMode, LocalNode, LocalPath, Initialize string
	Adopted                                            bool
}

func (v Volume) Local() bool { return v.LocalNode != "" || v.LocalPath != "" }
func (v Volume) Validate() error {
	if v.Name == "" {
		return fmt.Errorf("volume name cannot be empty")
	}
	if v.Local() && (!path.IsAbs(v.LocalPath) || path.Clean(v.LocalPath) == "/" || slices.Contains(strings.Split(v.LocalPath, "/"), "..") || strings.ContainsAny(v.LocalPath, "\x00\r\n")) {
		return fmt.Errorf("volume %s local.path must be absolute, other than /, without .. or control characters", v.Name)
	}
	if v.AccessMode != "ReadWriteOnce" {
		return fmt.Errorf("volume %s uses node-local storage; only ReadWriteOnce is supported", v.Name)
	}
	if v.Initialize != "" && v.Initialize != "empty" {
		return fmt.Errorf("volume %s initialize only supports empty", v.Name)
	}
	return nil
}
