package credentials

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"slices"
)

type AgentIdentity struct {
	TokenHash  string
	Names, IDs []string
}

func AgentTokenHash(token string) string {
	digest := sha256.Sum256([]byte(token))
	return hex.EncodeToString(digest[:])
}
func AuthenticateAgent(identity AgentIdentity, token, name, id string) error {
	if token == "" || name == "" || identity.TokenHash == "" || subtle.ConstantTimeCompare([]byte(identity.TokenHash), []byte(AgentTokenHash(token))) != 1 {
		return ErrUnauthorized
	}
	if id != "" && len(identity.IDs) > 0 && !slices.Contains(identity.IDs, id) {
		return ErrUnauthorized
	}
	if !slices.Contains(identity.Names, name) && !slices.Contains(identity.IDs, name) && !(id != "" && slices.Contains(identity.IDs, id)) {
		return ErrUnauthorized
	}
	return nil
}
