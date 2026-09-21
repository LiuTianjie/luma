package controlstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"

	"github.com/LiuTianjie/luma/internal/deployment"
)

func deploymentEgress(ctx context.Context, tx *sql.Tx) (deployment.EgressSettings, error) {
	s := deployment.EgressSettings{Regions: map[string]string{}}
	regions, err := configDocument(ctx, tx, "regions", 1<<20)
	if err != nil {
		return s, err
	}
	for name, raw := range regions {
		var record document
		if json.Unmarshal(raw, &record) != nil || record == nil {
			continue
		}
		s.Regions[name] = record.text("egress")
	}
	for key, dest := range map[string]*string{"nomadRpcAddr": &s.NomadRPCAddr, "nomadServerAddr": &s.NomadServerAddr, "nomadAddr": &s.NomadHTTPAddr} {
		var raw []byte
		err := tx.QueryRowContext(ctx, `SELECT CASE WHEN length(CAST(payload AS BLOB))<=4096 THEN payload ELSE NULL END FROM control_config WHERE key=?`, key).Scan(&raw)
		if errors.Is(err, sql.ErrNoRows) {
			continue
		}
		if err != nil {
			return s, err
		}
		if json.Unmarshal(raw, dest) != nil {
			return s, errors.New("invalid Control gateway configuration")
		}
	}
	return s, nil
}
