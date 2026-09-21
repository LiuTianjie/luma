package imageexec

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

type registryAuth struct {
	Username      string `json:"username"`
	Password      string `json:"password"`
	ServerAddress string `json:"serverAddress"`
	LegacyAddress string `json:"serveraddress"`
}
type authSession struct {
	path    string
	secrets []string
}

func newAuthSession(root string, auths ...*registryAuth) (*authSession, error) {
	// Always supply a private config, even for public images. Never consume the
	// machine owner's ambient Docker login or credential-helper configuration.
	if root == "" {
		return nil, fmt.Errorf("agent scratch directory is unavailable")
	}
	entries := map[string]map[string]string{}
	var secrets []string
	for _, auth := range auths {
		if auth == nil {
			continue
		}
		server := auth.ServerAddress
		if server == "" {
			server = auth.LegacyAddress
		}
		if auth.Username == "" || auth.Password == "" || server == "" || len(auth.Password) > 64<<10 || strings.ContainsAny(server, "\r\n\x00") {
			return nil, fmt.Errorf("invalid registry credentials")
		}
		encoded := base64.StdEncoding.EncodeToString([]byte(auth.Username + ":" + auth.Password))
		if previous, ok := entries[server]; ok && previous["auth"] != encoded {
			return nil, fmt.Errorf("conflicting credentials for the same registry")
		}
		entries[server] = map[string]string{"auth": encoded}
		secrets = append(secrets, auth.Password, auth.Username+":"+auth.Password, encoded)
	}
	path, err := os.MkdirTemp(root, "image-auth-")
	if err != nil {
		return nil, fmt.Errorf("create private image credentials: %w", err)
	}
	s := &authSession{path: path, secrets: secrets}
	data, err := json.Marshal(map[string]any{"auths": entries})
	if err == nil {
		err = os.WriteFile(filepath.Join(path, "config.json"), data, 0600)
	}
	if err != nil {
		s.close()
		return nil, fmt.Errorf("write private image credentials: %w", err)
	}
	return s, nil
}
func (s *authSession) close() { _ = os.RemoveAll(s.path) }
func (s *authSession) clean(value string) string {
	// Runner must redact before output truncation/progress framing. This second
	// pass also covers adapter-generated errors and results from other runners.
	for _, secret := range s.secrets {
		if secret != "" {
			value = strings.ReplaceAll(value, secret, "[REDACTED]")
		}
	}
	return value
}
