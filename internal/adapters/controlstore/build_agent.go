package controlstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"path"
	"strings"

	builddomain "github.com/LiuTianjie/luma/internal/build"
	"github.com/LiuTianjie/luma/internal/node"
)

// A task marker alone is not authority: both sides must reference one another
// and the authenticated node must own the build's selected executor.
func buildTaskOwner(ctx context.Context, tx *sql.Tx, task node.StoredTask, leasing bool) error {
	if !task.GoBuild || task.Action != "build-image" || task.BuildRunID == "" || task.BuilderTaskID != "" {
		return node.ErrActionNotMigrated
	}
	r, err := readBuild(ctx, tx, task.BuildRunID)
	if err != nil {
		return err
	}
	if !r.GoBuild || r.AgentTaskID != task.ID || r.BuildNode != task.NodeName {
		return node.ErrTaskState
	}
	if leasing && (!r.QueueExecuting || r.QueueOwnerReleased || r.Status != "running" || r.CancelRequestedAt != 0) {
		return node.ErrTaskState
	}
	return nil
}

func (s *Store) BuildTask(ctx context.Context, id string) (node.StoredTask, error) {
	var task node.StoredTask
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		r, err := readBuild(ctx, tx, id)
		if err != nil {
			return err
		}
		var raw []byte
		if err := tx.QueryRowContext(ctx, `SELECT payload FROM control_entities WHERE kind='agentTasks' AND id=?`, r.AgentTaskID).Scan(&raw); err != nil {
			return err
		}
		if json.Unmarshal(raw, &task) != nil {
			return node.ErrTaskState
		}
		return buildTaskOwner(ctx, tx, task, false)
	})
	return task, err
}

func providerCloneBase(p document) (string, error) {
	base := strings.TrimRight(strings.TrimSpace(p.text("cloneBaseUrl")), "/")
	if base == "" {
		switch p.text("type") {
		case "github":
			base = "https://github.com"
		case "gitea":
			base = strings.TrimRight(p.text("baseUrl"), "/")
		default:
			return "", fmt.Errorf("unsupported Git provider")
		}
	}
	u, err := url.Parse(base)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return "", fmt.Errorf("invalid Git provider clone base")
	}
	return base, nil
}

func (a *agentTx) buildLeasePayload(task node.StoredTask, payload document) (json.RawMessage, error) {
	if err := buildTaskOwner(a.ctx, a.tx, task, true); err != nil {
		return nil, err
	}
	repo, err := url.Parse(payload.text("repoUrl"))
	if err != nil {
		return nil, builddomain.ErrInput
	}
	if id := payload.text("gitProviderId"); id != "" {
		providers, err := configDocument(a.ctx, a.tx, "gitProviders", 8<<20)
		if err != nil {
			return nil, err
		}
		provider := providers.object(id)
		base, err := providerCloneBase(provider)
		if err != nil {
			return nil, err
		}
		origin, _ := url.Parse(base)
		// A provider credential must not be sent to a caller-selected foreign host
		// or outside an explicitly configured provider URL prefix.
		if repo.Scheme != origin.Scheme || !strings.EqualFold(repo.Host, origin.Host) || !strings.HasPrefix(path.Clean(repo.Path), strings.TrimRight(path.Clean("/"+strings.TrimLeft(origin.Path, "/")), "/")+"/") {
			return nil, errors.New("repository does not belong to the selected Git provider")
		}
		if token := provider.text("token"); token != "" {
			_ = payload.set("gitToken", token)
		}
		if username := provider.text("username"); username != "" {
			_ = payload.set("gitUsername", username)
		}
	} else if repo.Scheme == "https" && strings.EqualFold(repo.Host, "github.com") {
		secrets, err := configDocument(a.ctx, a.tx, "secrets", 8<<20)
		if err != nil {
			return nil, err
		}
		if token := secrets.text("GITHUB_TOKEN"); token != "" {
			_ = payload.set("gitToken", token)
		}
	}
	catalog, err := configDocument(a.ctx, a.tx, "registries", 8<<20)
	if err != nil {
		return nil, err
	}
	push, public, imageRepo := payload.text("pushHost"), payload.text("registryHost"), payload.text("repo")
	auth, err := registryAuth(catalog, push+"/"+imageRepo+":latest")
	if err != nil {
		return nil, err
	}
	if auth == nil {
		auth, err = registryAuth(catalog, public+"/"+imageRepo+":latest")
	}
	if err != nil {
		return nil, err
	}
	if auth != nil {
		auth["serveraddress"] = push
		_ = payload.set("registryAuth", auth)
	}
	return json.Marshal(payload)
}
