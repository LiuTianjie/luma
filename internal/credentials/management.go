// Package credentials owns principal validation, without HTTP or storage I/O.
package credentials

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"errors"
)

var ErrUnauthorized = errors.New("unauthorized")

type ManagementIdentity struct {
	ClusterID string
	Endpoint  string
	Token     string // Internal only; never serialize a stored identity to clients.
}

type ManagementRepository interface {
	ManagementIdentity(context.Context) (ManagementIdentity, error)
}

type Session struct {
	ClusterID string `json:"clusterId"`
	Endpoint  string `json:"endpoint"`
}

func Authenticate(ctx context.Context, repository ManagementRepository, token string) (Session, error) {
	identity, err := repository.ManagementIdentity(ctx)
	if err != nil {
		return Session{}, err
	}
	return AuthenticateManagement(identity, token)
}

// AuthenticateManagement is also used inside a repository write transaction,
// so a token rotation cannot race a previously authorized state mutation.
func AuthenticateManagement(identity ManagementIdentity, token string) (Session, error) {
	expected, supplied := sha256.Sum256([]byte(identity.Token)), sha256.Sum256([]byte(token))
	if identity.Token == "" || token == "" || subtle.ConstantTimeCompare(expected[:], supplied[:]) != 1 {
		return Session{}, ErrUnauthorized
	}
	return Session{ClusterID: identity.ClusterID, Endpoint: identity.Endpoint}, nil
}
