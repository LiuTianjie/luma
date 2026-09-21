package controlstore

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/deployment"
	"github.com/LiuTianjie/luma/internal/registry"
	"github.com/LiuTianjie/luma/internal/storage"
)

func configDocument(ctx context.Context, tx *sql.Tx, key string, limit int) (document, error) {
	var raw []byte
	err := tx.QueryRowContext(ctx, `SELECT CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload ELSE NULL END FROM control_config WHERE key=?`, limit, key).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return document{}, nil
	}
	if err != nil {
		return nil, err
	}
	return readDocument(raw)
}
func deploymentBucket(kind string) string {
	if kind == "compose" {
		return "compose"
	}
	return "services"
}
func deploymentRevision(raw json.RawMessage) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
func deploymentState(ctx context.Context, tx *sql.Tx, kind, slug string) (deployment.Snapshot, document, error) {
	s := deployment.Snapshot{NodeAddresses: map[string]deployment.NodeAddress{}, ImageNodes: map[string]deployment.ImageNode{}}
	d, err := configDocument(ctx, tx, "deployments", 32<<20)
	if err != nil {
		return s, nil, err
	}
	s.RelayReservations, err = relayReservations(d)
	if err != nil {
		return s, nil, err
	}
	bucket := deploymentBucket(kind)
	other := "compose"
	if bucket == "compose" {
		other = "services"
	}
	if _, ok := d.object(other)[slug]; ok {
		return s, nil, deployment.ErrConflict
	}
	raw := d.object(bucket)[slug]
	s.Revision = deploymentRevision(raw)
	if len(raw) > 0 {
		var r deployment.Record
		if json.Unmarshal(raw, &r) != nil {
			return s, nil, errors.New("invalid deployment record")
		}
		s.Previous = &r
	}
	var clusterRaw []byte
	if err := tx.QueryRowContext(ctx, `SELECT payload FROM control_config WHERE key='clusterId'`).Scan(&clusterRaw); err != nil {
		return s, nil, err
	}
	if json.Unmarshal(clusterRaw, &s.ClusterID) != nil {
		return s, nil, errors.New("invalid cluster identity")
	}
	s.Egress, err = deploymentEgress(ctx, tx)
	if err != nil {
		return s, nil, err
	}
	build, err := configDocument(ctx, tx, "build", 1<<20)
	if err != nil {
		return s, nil, err
	}
	builders := build.strings("nodes")
	s.Cache = registry.CacheConfig{PullHost: build.text("registryHost"), PushHost: build.text("pushHost"), Builder: build.text("defaultNode")}
	for key, v := range build.object("nodes") {
		if string(v) != "false" && string(v) != "null" {
			builders = append(builders, key)
		}
	}
	if name := strings.TrimSpace(build.text("defaultNode")); name != "" {
		builders = append(builders, name)
	}
	rows, err := tx.QueryContext(ctx, `SELECT id,CASE WHEN length(CAST(payload AS BLOB))<=262144 THEN payload ELSE NULL END FROM control_entities WHERE kind='nodes' ORDER BY id LIMIT 10001`)
	if err != nil {
		return s, nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var key string
		var raw []byte
		if err := rows.Scan(&key, &raw); err != nil {
			return s, nil, err
		}
		r, err := readDocument(raw)
		if err != nil {
			return s, nil, err
		}
		labels := r.object("labels")
		id := r.text("nodeId")
		if id == "" {
			id = r.text("nomadNodeId")
		}
		if id == "" {
			id = labels.text("luma.node.id")
		}
		ready := deploymentNodeReady(r)
		aliases := append(r.strings("aliases"), r.text("name"), r.text("displayName"), r.text("hostname"), labels.text("luma.node.name"), labels.text("luma_node_name"))
		address := deployment.NodeAddress{Region: r.text("region"), Roles: r.strings("roles"), LocalIngress: r.text("status") == "manager" || r.text("nomadRole") == "server" || r.boolean("nomadServer") || labels.text("role.nomad-manager") == "true"}
		for _, field := range []string{"tailscaleIP", "tailscaleIp", "tailscaleName", "advertiseAddr", "publicIp", "public_ip", "address", "hostname", "name", "displayName"} {
			if v := r.text(field); v != "" {
				address.Address = v
				break
			}
		}
		address.PublicIP = r.text("publicIp")
		address.Tailnet = r.text("tailscaleIP") != "" || r.text("tailscaleIp") != "" || r.text("tailscaleName") != ""
		if address.PublicIP == "" {
			address.PublicIP = r.text("public_ip")
		}
		if r.text("status") == "manager" || slices.Contains(r.strings("roles"), "nomad-manager") || strings.EqualFold(labels.text("role.nomad-manager"), "true") {
			for _, field := range []string{"tailscaleIP", "advertiseAddr", "publicIp", "publicIP"} {
				if host := strings.TrimSpace(r.text(field)); host != "" {
					s.Egress.ManagerHosts = append(s.Egress.ManagerHosts, host)
					break
				}
			}
		}
		if address.Address == "" {
			address.Address = key
		}
		s.NodeAddresses[key] = address
		imageNode := deploymentImageNode(key, r, build, builders, aliases, ready)
		s.ImageNodes[key] = imageNode
		s.Nodes = append(s.Nodes, storage.Node{Name: key, ID: id, Region: r.text("region"), Aliases: aliases, Ready: ready, Builder: imageNode.Builder})
		if len(s.Nodes) > 10000 {
			return s, nil, errors.New("too many node records")
		}
	}
	return s, d, rows.Err()
}
func (s *Store) DeploymentSnapshot(ctx context.Context, kind, slug string) (deployment.Snapshot, error) {
	var result deployment.Snapshot
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		var err error
		result, _, err = deploymentState(ctx, tx, kind, slug)
		return err
	})
	return result, err
}
func checkManagement(ctx context.Context, tx *sql.Tx, token string) error {
	var raw []byte
	var expected string
	if err := tx.QueryRowContext(ctx, `SELECT payload FROM control_config WHERE key='deployToken'`).Scan(&raw); err != nil {
		return err
	}
	if json.Unmarshal(raw, &expected) != nil {
		return errors.New("invalid management identity")
	}
	_, err := credentials.AuthenticateManagement(credentials.ManagementIdentity{Token: expected}, token)
	return err
}
func writeDeployment(ctx context.Context, tx *sql.Tx, d document, r deployment.Record) error {
	key := deploymentBucket(r.Kind)
	bucket := d.object(key)
	old := document{}
	if raw := bucket[r.Slug]; len(raw) > 0 {
		var err error
		old, err = readDocument(raw)
		if err != nil {
			return err
		}
	}
	raw, err := json.Marshal(r)
	if err != nil {
		return err
	}
	updated, err := readDocument(raw)
	if err != nil {
		return err
	}
	for k, v := range updated {
		old[k] = v
	}
	raw, err = json.Marshal(old)
	if err != nil {
		return err
	}
	bucket[r.Slug] = raw
	if err = d.set(key, bucket); err != nil {
		return err
	}
	encoded, err := json.Marshal(d)
	if err != nil {
		return err
	}
	if len(encoded) > 32<<20 {
		return errors.New("deployment catalog exceeds schema 1 limits")
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO control_config(key,payload) VALUES('deployments',?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload`, string(encoded))
	return err
}
func (s *Store) BeginDeployment(ctx context.Context, token, revision string, r deployment.Record, imports map[string]string) error {
	if s.writer == nil {
		return ErrReadOnly
	}
	return s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		if err := checkManagement(ctx, tx, token); err != nil {
			return err
		}
		current, d, err := deploymentState(ctx, tx, r.Kind, r.Slug)
		if err != nil {
			return err
		}
		if current.Revision != revision || current.Previous != nil && current.Previous.Status == "pending" {
			return deployment.ErrConflict
		}
		if err := deployment.CheckRelayPorts(r.Kind, r.Slug, r.TCPRelayPorts, current.RelayReservations); err != nil {
			return err
		}
		if r.NodeID != "" {
			valid := false
			for _, n := range current.Nodes {
				if n.Name == r.Node && n.ID == r.NodeID && n.Ready && n.Region == r.Region {
					valid = true
				}
			}
			if !valid {
				return deployment.ErrConflict
			}
		}
		if err := importDeploymentSecrets(ctx, tx, r.Slug, imports); err != nil {
			return err
		}
		if err := writeDeployment(ctx, tx, d, r); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT OR IGNORE INTO control_collections(kind,shape) VALUES('deploymentEvents','list')`); err != nil {
			return err
		}
		event := map[string]any{"id": r.OperationID, "kind": r.Kind, "name": r.Name, "slug": r.Slug, "sourceName": r.SourceName, "origin": r.Origin, "status": "pending", "stepCount": 0, "createdAt": r.UpdatedAt, "goDeployment": true}
		raw, err := json.Marshal(event)
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO control_entities(kind,id,payload,status,app,created_at,updated_at,ordinal) VALUES('deploymentEvents',?,?,'pending',?,?,?,(SELECT COALESCE(MAX(ordinal),-1)+1 FROM control_entities WHERE kind='deploymentEvents'))`, r.OperationID, string(raw), r.Slug, r.UpdatedAt, r.UpdatedAt)
		return err
	})
}
func finishDeployment(ctx context.Context, tx *sql.Tx, d document, r deployment.Record) error {
	current := d.object(deploymentBucket(r.Kind)).object(r.Slug)
	if current.text("operationId") != r.OperationID || current.text("status") != "pending" {
		return deployment.ErrConflict
	}
	if r.Status != "active" {
		if err := cancelDeploymentTasks(ctx, tx, r); err != nil {
			return err
		}
	}
	if err := writeDeployment(ctx, tx, d, r); err != nil {
		return err
	}
	var raw []byte
	if err := tx.QueryRowContext(ctx, `SELECT payload FROM control_entities WHERE kind='deploymentEvents' AND id=?`, r.OperationID).Scan(&raw); err != nil {
		return err
	}
	event, err := readDocument(raw)
	if err != nil {
		return err
	}
	for key, value := range map[string]any{"status": r.Status, "error": r.LastError, "completedAt": r.UpdatedAt, "runtime": r.Runtime} {
		if err = event.set(key, value); err != nil {
			return err
		}
	}
	raw, err = json.Marshal(event)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE control_entities SET payload=?,status=?,updated_at=? WHERE kind='deploymentEvents' AND id=?`, string(raw), r.Status, r.UpdatedAt, r.OperationID)
	return err
}
func (s *Store) FinishDeployment(ctx context.Context, r deployment.Record) error {
	if s.writer == nil {
		return ErrReadOnly
	}
	return s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		d, err := configDocument(ctx, tx, "deployments", 32<<20)
		if err != nil {
			return err
		}
		return finishDeployment(ctx, tx, d, r)
	})
}
func (s *Store) RecoverDeployments(ctx context.Context) error {
	if s.writer == nil {
		return ErrReadOnly
	}
	return s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		d, err := configDocument(ctx, tx, "deployments", 32<<20)
		if err != nil {
			return err
		}
		for _, bucket := range []string{"services", "compose"} {
			for _, raw := range d.object(bucket) {
				var r deployment.Record
				if json.Unmarshal(raw, &r) != nil {
					return errors.New("invalid deployment record")
				}
				if r.Status != "pending" || !strings.HasPrefix(r.OperationID, "deploy-") {
					continue
				}
				var marker bool
				if err := tx.QueryRowContext(ctx, `SELECT COALESCE(json_extract(payload,'$.goDeployment'),0) FROM control_entities WHERE kind='deploymentEvents' AND id=?`, r.OperationID).Scan(&marker); err != nil {
					if errors.Is(err, sql.ErrNoRows) {
						continue
					}
					return err
				}
				if !marker {
					continue
				}
				r.Status = "failed_partial"
				r.LastError = "Control restarted during deployment; Nomad may still be running this revision; inspect before retrying"
				r.UpdatedAt = time.Now().Unix()
				if err := finishDeployment(ctx, tx, d, r); err != nil {
					return fmt.Errorf("recover interrupted deployment: %w", err)
				}
			}
		}
		return nil
	})
}
