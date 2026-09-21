package controlstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"

	"github.com/LiuTianjie/luma/internal/credentials"
	"github.com/LiuTianjie/luma/internal/workflow"
)

// Schema 1 stores the workflow catalog as a single configuration value. Keep
// that representation readable by Python, and bound it before decoding. Queue
// and history entities are never materialized by this repository.
const maxWorkflowCatalog = 16 << 20

func readWorkflowCatalog(ctx context.Context, tx *sql.Tx) (workflow.Catalog, document, error) {
	c := workflow.Catalog{Records: map[string]workflow.Record{}, ProviderBases: map[string]string{}}
	d := document{}
	var raw []byte
	err := tx.QueryRowContext(ctx, `SELECT CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload ELSE NULL END FROM control_config WHERE key='deploymentWorkflows'`, maxWorkflowCatalog).Scan(&raw)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return c, nil, err
	}
	if err == nil {
		var parseErr error
		d, parseErr = readDocument(raw)
		if parseErr != nil || len(d) > 10000 {
			return c, nil, errors.New("invalid or oversized workflow catalog")
		}
		for key, value := range d {
			var r workflow.Record
			if json.Unmarshal(value, &r) != nil || r.Name == "" || r.Revision < 0 || r.Slug != key {
				return c, nil, errors.New("invalid stored workflow record")
			}
			c.Records[key] = r
		}
	}
	// Project only provider names and base URLs, never provider credentials.
	rows, err := tx.QueryContext(ctx, `SELECT j.key,COALESCE(NULLIF(json_extract(j.value,'$.cloneBaseUrl'),''), CASE WHEN json_extract(j.value,'$.type')='github' THEN 'https://github.com' ELSE json_extract(j.value,'$.baseUrl') END) FROM control_config,json_each(control_config.payload) AS j WHERE control_config.key='gitProviders' LIMIT 10001`)
	if err != nil {
		return c, nil, err
	}
	defer rows.Close()
	count := 0
	for rows.Next() {
		var key string
		var base sql.NullString
		if err := rows.Scan(&key, &base); err != nil {
			return c, nil, err
		}
		count++
		if count > 10000 || len(base.String) > 4096 {
			return c, nil, errors.New("oversized workflow provider catalog")
		}
		c.ProviderBases[key] = base.String
	}
	return c, d, rows.Err()
}

func (s *Store) Workflows(ctx context.Context) (workflow.Catalog, error) {
	var c workflow.Catalog
	err := s.snapshot(ctx, func(tx *sql.Tx) error {
		var err error
		c, _, err = readWorkflowCatalog(ctx, tx)
		return err
	})
	return c, err
}

func (s *Store) UpdateWorkflow(ctx context.Context, token string, edit func(workflow.Catalog) (workflow.Record, error)) (workflow.Record, error) {
	var result workflow.Record
	if s.writer == nil {
		return result, ErrReadOnly
	}
	err := s.transaction(ctx, s.writer, false, func(tx *sql.Tx) error {
		var rawToken []byte
		if err := tx.QueryRowContext(ctx, `SELECT payload FROM control_config WHERE key='deployToken'`).Scan(&rawToken); err != nil {
			return err
		}
		var stored string
		if json.Unmarshal(rawToken, &stored) != nil {
			return errors.New("invalid management identity")
		}
		if _, err := credentials.AuthenticateManagement(credentials.ManagementIdentity{Token: stored}, token); err != nil {
			return err
		}
		catalog, documents, err := readWorkflowCatalog(ctx, tx)
		if err != nil {
			return err
		}
		result, err = edit(catalog)
		if err != nil {
			return err
		}
		if result.Slug == "" || result.Slug != workflow.Slug(result.Name) {
			return errors.New("invalid workflow identity")
		}
		raw, err := json.Marshal(result)
		if err != nil {
			return err
		}
		// Preserve extension fields on the edited legacy record and all unrelated
		// records. The domain owns only fields represented by workflow.Record.
		updated, err := readDocument(raw)
		if err != nil {
			return err
		}
		previous := document{}
		if old, ok := documents[result.Slug]; ok {
			previous, err = readDocument(old)
			if err != nil {
				return err
			}
		}
		for key, value := range updated {
			previous[key] = value
		}
		raw, err = json.Marshal(previous)
		if err != nil {
			return err
		}
		documents[result.Slug] = raw
		encoded, err := json.Marshal(documents)
		if err != nil {
			return err
		}
		if len(encoded) > maxWorkflowCatalog || len(documents) > 10000 {
			return errors.New("workflow catalog exceeds schema 1 limits")
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO control_config(key,payload) VALUES('deploymentWorkflows',?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload`, string(encoded))
		return err
	})
	return result, err
}
