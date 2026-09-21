package credentials

import (
	"context"
	"errors"
	"testing"
)

type identityRepo struct {
	identity ManagementIdentity
	err      error
}

func (r identityRepo) ManagementIdentity(context.Context) (ManagementIdentity, error) {
	return r.identity, r.err
}
func TestPrincipalSeparationAndUnavailableStore(t *testing.T) {
	repo := identityRepo{identity: ManagementIdentity{Token: "management", ClusterID: "cluster", Endpoint: "example.test"}}
	for _, token := range []string{"", "join-token", "node-token", "management-extra"} {
		if _, err := Authenticate(context.Background(), repo, token); !errors.Is(err, ErrUnauthorized) {
			t.Fatal(token, err)
		}
	}
	session, err := Authenticate(context.Background(), repo, "management")
	if err != nil || session.ClusterID != "cluster" {
		t.Fatal(session, err)
	}
	repo.identity.Token = ""
	if _, err := Authenticate(context.Background(), repo, ""); !errors.Is(err, ErrUnauthorized) {
		t.Fatal(err)
	}
	broken := errors.New("unavailable")
	repo.err = broken
	if _, err := Authenticate(context.Background(), repo, "management"); !errors.Is(err, broken) {
		t.Fatal(err)
	}
}
