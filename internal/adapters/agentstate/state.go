// Package agentstate keeps private agent configuration and terminal receipts.
// It never stores task payloads or execution environments.
package agentstate

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"

	"github.com/LiuTianjie/luma/internal/node"
)

const maxBytes = 1 << 20

type Config struct {
	Endpoint                     string  `json:"endpoint"`
	Token                        string  `json:"token"`
	NodeName                     string  `json:"nodeName"`
	NodeID                       string  `json:"nodeId"`
	Insecure                     bool    `json:"insecure"`
	ResolveIP                    string  `json:"resolveIp"`
	PollIntervalSeconds          int     `json:"pollIntervalSeconds"`
	StatsIntervalSeconds         int     `json:"statsIntervalSeconds"`
	BusyHeartbeatIntervalSeconds float64 `json:"busyHeartbeatIntervalSeconds"`
}

func LoadConfig(path string) (Config, error) {
	var config Config
	data, err := readPrivate(path)
	if err != nil {
		return config, err
	}
	if err := json.Unmarshal(data, &config); err != nil {
		return config, fmt.Errorf("invalid agent configuration JSON")
	}
	if config.Endpoint == "" || config.Token == "" || config.NodeName == "" {
		return config, fmt.Errorf("agent config requires endpoint, token and nodeName")
	}
	if config.PollIntervalSeconds == 0 {
		config.PollIntervalSeconds = 5
	}
	if config.PollIntervalSeconds < 1 || config.PollIntervalSeconds > 300 {
		return config, fmt.Errorf("pollIntervalSeconds must be between 1 and 300")
	}
	if config.StatsIntervalSeconds == 0 {
		config.StatsIntervalSeconds = 30
	}
	if config.StatsIntervalSeconds < 1 || config.StatsIntervalSeconds > 3600 {
		return config, fmt.Errorf("statsIntervalSeconds must be between 1 and 3600")
	}
	if config.BusyHeartbeatIntervalSeconds == 0 {
		config.BusyHeartbeatIntervalSeconds = 2
	}
	if config.BusyHeartbeatIntervalSeconds < 0.01 || config.BusyHeartbeatIntervalSeconds > 300 {
		return config, fmt.Errorf("busyHeartbeatIntervalSeconds must be between 0.01 and 300")
	}
	return config, nil
}
func (c Config) WorkerOptions() node.WorkerOptions {
	return node.WorkerOptions{PollInterval: time.Duration(c.PollIntervalSeconds) * time.Second, LeaseWait: time.Duration(min(30, max(10, c.PollIntervalSeconds))) * time.Second, HeartbeatInterval: time.Duration(min(c.BusyHeartbeatIntervalSeconds, 2) * float64(time.Second))}
}

type Store struct{ path string }

func Open(configPath string) (*Store, func(), error) {
	path := configPath + ".go-receipt.json"
	unlock, err := lock(configPath + ".go-agent.lock")
	if err != nil {
		return nil, nil, err
	}
	return &Store{path: path}, unlock, nil
}
func (s *Store) Load() (*node.Receipt, error) {
	data, err := readPrivate(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var receipt node.Receipt
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if err := d.Decode(&receipt); err != nil {
		return nil, fmt.Errorf("invalid terminal receipt JSON")
	}
	if d.Decode(new(any)) != io.EOF {
		return nil, fmt.Errorf("invalid trailing terminal receipt data")
	}
	if err := receipt.Validate(); err != nil {
		return nil, err
	}
	if !json.Valid(receipt.Result) || len(receipt.Result) == 0 || bytes.TrimSpace(receipt.Result)[0] != '{' {
		return nil, fmt.Errorf("invalid terminal receipt result")
	}
	return &receipt, nil
}
func (s *Store) Save(receipt node.Receipt) error {
	if err := receipt.Validate(); err != nil {
		return err
	}
	if !json.Valid(receipt.Result) || len(receipt.Result) == 0 || bytes.TrimSpace(receipt.Result)[0] != '{' {
		return fmt.Errorf("invalid terminal receipt result")
	}
	data, err := json.Marshal(receipt)
	if err != nil {
		return err
	}
	if len(data) > maxBytes {
		return fmt.Errorf("terminal receipt exceeds limit")
	}
	f, err := os.CreateTemp(filepath.Dir(s.path), ".agent-receipt-*")
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
	if err := os.Rename(f.Name(), s.path); err != nil {
		return err
	}
	return syncDirectory(filepath.Dir(s.path))
}
func (s *Store) Clear() error {
	if err := os.Remove(s.path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return syncDirectory(filepath.Dir(s.path))
}
func readPrivate(path string) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		return nil, fmt.Errorf("agent state must be a regular private file (mode 0600): %s", path)
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, maxBytes+1))
	if len(data) > maxBytes {
		return nil, fmt.Errorf("agent state exceeds limit")
	}
	return data, err
}
func syncDirectory(path string) error {
	dir, err := os.Open(path)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}
