// Package contexts reads and writes the existing Python CLI context format.
package contexts

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"unicode"
)

type Context struct {
	Endpoint  string `json:"endpoint"`
	ClusterID string `json:"clusterId"`
	Token     string `json:"token,omitempty"`
	Insecure  bool   `json:"insecure"`
	ResolveIP string `json:"resolveIp,omitempty"`
	Current   bool   `json:"current,omitempty"`
}
type Store struct{ Root string }

func New(root string) (Store, error) {
	if root == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return Store{}, err
		}
		root = filepath.Join(home, ".config", "luma")
	}
	return Store{Root: root}, nil
}
func (s Store) path(name string) (string, error) {
	if name == "" {
		return "", fmt.Errorf("invalid cluster id")
	}
	for _, r := range name {
		if !unicode.IsLetter(r) && !unicode.IsDigit(r) && !strings.ContainsRune("-_.", r) {
			return "", fmt.Errorf("invalid cluster id")
		}
	}
	return filepath.Join(s.Root, "contexts", name+".json"), nil
}
func (s Store) Load(name string) (Context, error) {
	if name == "" {
		data, err := os.ReadFile(filepath.Join(s.Root, "current-context"))
		if err != nil {
			return Context{}, fmt.Errorf("not logged in: run luma-go login <control-url>")
		}
		name = strings.TrimSpace(string(data))
	}
	path, err := s.path(name)
	if err != nil {
		return Context{}, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return Context{}, fmt.Errorf("context not found: %s", name)
	}
	var value Context
	if err := json.Unmarshal(data, &value); err != nil || value.Endpoint == "" || value.Token == "" {
		return Context{}, fmt.Errorf("invalid context: %s", name)
	}
	return value, nil
}
func (s Store) Save(value Context) error {
	path, err := s.path(value.ClusterID)
	if err != nil {
		return err
	}
	value.Endpoint = strings.TrimRight(value.Endpoint, "/")
	value.Current = false
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	if err := atomicWrite(path, append(data, '\n')); err != nil {
		return err
	}
	return s.Use(value.ClusterID)
}
func (s Store) Use(name string) error {
	if _, err := s.Load(name); err != nil {
		return err
	}
	return atomicWrite(filepath.Join(s.Root, "current-context"), []byte(name+"\n"))
}
func (s Store) List() ([]Context, error) {
	files, err := filepath.Glob(filepath.Join(s.Root, "contexts", "*.json"))
	if err != nil {
		return nil, err
	}
	current, _ := os.ReadFile(filepath.Join(s.Root, "current-context"))
	result := []Context{}
	for _, path := range files {
		value, err := s.Load(strings.TrimSuffix(filepath.Base(path), ".json"))
		if err != nil {
			continue
		}
		value.Token = ""
		value.Current = value.ClusterID == strings.TrimSpace(string(current))
		result = append(result, value)
	}
	return result, nil
}
func atomicWrite(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".luma-context-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	defer f.Close()
	if err := f.Chmod(0600); err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		return err
	}
	if err := f.Sync(); err != nil {
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(f.Name(), path); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}
