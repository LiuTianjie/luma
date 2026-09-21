package registry

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
)

var ErrCredentialInput = errors.New("invalid registry credential request")
var credentialHost = regexp.MustCompile(`^[A-Za-z0-9._:-]+$`)

type CredentialChange struct {
	Host          string `json:"host"`
	ServerAddress string `json:"serverAddress,omitempty"`
	Username      string `json:"username,omitempty"`
	Password      string `json:"password,omitempty"`
}
type CredentialInfo struct {
	Host          string `json:"host"`
	ServerAddress string `json:"serverAddress"`
	Username      string `json:"username"`
	Configured    bool   `json:"configured"`
}
type CredentialResult struct {
	Host     string `json:"host"`
	Username string `json:"username,omitempty"`
	Saved    *bool  `json:"saved,omitempty"`
	Removed  *bool  `json:"removed,omitempty"`
}
type CredentialRepository interface {
	RegistryCredentials(context.Context) ([]CredentialInfo, error)
	ChangeRegistryCredential(context.Context, string, CredentialChange, bool) (CredentialResult, error)
}

func ValidateCredential(c CredentialChange, remove bool) (CredentialChange, error) {
	if c.Host == "" && !remove {
		c.Host = c.ServerAddress
	}
	c.Host = strings.ToLower(strings.TrimRight(strings.TrimSpace(c.Host), "/"))
	if len(c.Host) > 1024 || !credentialHost.MatchString(c.Host) {
		return c, fmt.Errorf("%w: registry host must be a hostname with optional port, without a URL scheme or path", ErrCredentialInput)
	}
	c.ServerAddress = c.Host
	c.Username = strings.TrimSpace(c.Username)
	if !remove && (c.Username == "" || c.Password == "") {
		return c, fmt.Errorf("%w: registry username and password are required", ErrCredentialInput)
	}
	if len(c.Username) > 4096 || len(c.Password) > 1<<20 {
		return c, fmt.Errorf("%w: registry username or password exceeds its size limit", ErrCredentialInput)
	}
	return c, nil
}
