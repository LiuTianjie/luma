package clickhouse

import (
	"context"
	"fmt"
	"regexp"
)

type PartitionStat struct {
	Table     string `json:"table"`
	Partition string `json:"partition"`
	Bytes     uint64 `json:"bytes"`
}

var (
	droppableTables = map[string]bool{
		"replay_segments": true,
		"logs":            true,
		"events":          true,
		"exceptions":      true,
		"vitals":          true,
		"events_hourly":   true,
	}
	validPartitionID = regexp.MustCompile(`^[0-9]{6,8}$`)
)

func (c *Client) StorageSnapshot(ctx context.Context) (int64, []PartitionStat, error) {
	type sumRow struct {
		Bytes uint64 `json:"bytes"`
	}
	sumRows, err := QueryJSON[sumRow](ctx, c,
		"SELECT sum(bytes_on_disk) AS bytes FROM system.parts WHERE active = 1 AND database IN ({db:String}, 'system')",
		map[string]string{"db": c.db},
	)
	if err != nil {
		return 0, nil, err
	}
	var totalBytes int64
	if len(sumRows) > 0 {
		totalBytes = int64(sumRows[0].Bytes)
	}

	partitions, err := QueryJSON[PartitionStat](ctx, c,
		`SELECT table, partition, sum(bytes_on_disk) AS bytes
		 FROM system.parts
		 WHERE active = 1
		   AND database = {db:String}
		   AND table IN ('replay_segments', 'logs', 'events', 'exceptions', 'vitals', 'events_hourly')
		 GROUP BY table, partition
		 ORDER BY partition ASC, bytes DESC`,
		map[string]string{"db": c.db},
	)
	if err != nil {
		return totalBytes, nil, err
	}
	return totalBytes, partitions, nil
}

func (c *Client) DropPartition(ctx context.Context, table, partition string) error {
	if !droppableTables[table] {
		return fmt.Errorf("table %q is not eligible for partition drop", table)
	}
	if !validPartitionID.MatchString(partition) {
		return fmt.Errorf("invalid partition id %q", partition)
	}
	stmt := fmt.Sprintf("ALTER TABLE %s.%s DROP PARTITION '%s'", c.db, table, partition)
	return c.Exec(ctx, stmt)
}
