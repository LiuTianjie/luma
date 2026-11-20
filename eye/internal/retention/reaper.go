package retention

import (
	"context"
	"log"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"github.com/liutianjie/luma-eye/internal/clickhouse"
	"github.com/liutianjie/luma-eye/internal/config"
	"github.com/liutianjie/luma-eye/internal/project"
	"github.com/liutianjie/luma-eye/internal/sourcemap"
)

type StorageBackend interface {
	StorageSnapshot(ctx context.Context) (int64, []clickhouse.PartitionStat, error)
	DropPartition(ctx context.Context, table, partition string) error
}

type Status struct {
	RetentionDays     int       `json:"retention_days"`
	MaxStorageBytes   int64     `json:"max_storage_bytes"`
	ClickHouseBytes   int64     `json:"clickhouse_bytes"`
	DataDirBytes      int64     `json:"data_dir_bytes"`
	TotalBytes        int64     `json:"total_bytes"`
	DroppedPartitions uint64    `json:"dropped_partitions"`
	PrunedSourceMaps  uint64    `json:"pruned_sourcemaps"`
	LastRunAt         time.Time `json:"last_run_at,omitempty"`
}

type Reaper struct {
	cfg   *config.Config
	ch    StorageBackend
	store *project.Store
	sym   *sourcemap.Symbolicator

	mu     sync.RWMutex
	status Status
}

func NewReaper(cfg *config.Config, ch StorageBackend, store *project.Store, sym *sourcemap.Symbolicator) *Reaper {
	days := 7
	var maxBytes int64 = int64(15) << 30
	if cfg != nil {
		if cfg.RetentionDays > 0 {
			days = cfg.RetentionDays
		}
		maxBytes = cfg.MaxStorageBytes
	}
	return &Reaper{
		cfg:   cfg,
		ch:    ch,
		store: store,
		sym:   sym,
		status: Status{
			RetentionDays:   days,
			MaxStorageBytes: maxBytes,
		},
	}
}

func (r *Reaper) Snapshot() Status {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.status
}

func (r *Reaper) Start(ctx context.Context) {
	if r == nil {
		return
	}
	interval := 10 * time.Minute
	if r.cfg != nil && r.cfg.RetentionInterval > 0 {
		interval = r.cfg.RetentionInterval
	}
	go func() {
		initTimer := time.NewTimer(3 * time.Second)
		select {
		case <-ctx.Done():
			initTimer.Stop()
			return
		case <-initTimer.C:
		}
		if err := r.RunOnce(ctx, time.Now().UTC()); err != nil && ctx.Err() == nil {
			log.Printf("[luma-eye] retention reaper check failed: %v", err)
		}

		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if err := r.RunOnce(ctx, time.Now().UTC()); err != nil && ctx.Err() == nil {
					log.Printf("[luma-eye] retention reaper check failed: %v", err)
				}
			}
		}
	}()
}

func (r *Reaper) RunOnce(ctx context.Context, now time.Time) error {
	if now.IsZero() {
		now = time.Now().UTC()
	} else {
		now = now.UTC()
	}

	retentionDays := 7
	var maxBytes int64
	dataDir := ""
	if r.cfg != nil {
		if r.cfg.RetentionDays > 0 {
			retentionDays = r.cfg.RetentionDays
		}
		maxBytes = r.cfg.MaxStorageBytes
		dataDir = r.cfg.DataDir
	}
	maxAge := time.Duration(retentionDays) * 24 * time.Hour

	maxSourceMapsBytes := int64(1) << 30
	if maxBytes > 0 {
		tenth := maxBytes / 10
		if tenth < maxSourceMapsBytes {
			maxSourceMapsBytes = tenth
		}
		if maxSourceMapsBytes < 1<<20 {
			maxSourceMapsBytes = 1 << 20
		}
	}

	var prunedMaps int
	if r.sym != nil {
		n, _, err := r.sym.Prune(maxAge, maxSourceMapsBytes, 3)
		if err != nil {
			log.Printf("[luma-eye] prune sourcemaps failed: %v", err)
		}
		prunedMaps = n
	}
	if r.store != nil {
		_ = r.store.PruneStaleIssues(maxAge)
	}

	dataDirBytes := measureDirBytes(dataDir)

	var chBytes int64
	var dropped uint64
	var chErr error

	if r.ch != nil {
		snapCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
		totalCH, parts, err := r.ch.StorageSnapshot(snapCtx)
		cancel()
		if err != nil {
			chErr = err
		} else {
			chBytes = totalCH
			cutoffDay := now.AddDate(0, 0, -retentionDays).Format("20060102")
			cutoffMonth := cutoffDay[:6]

			remaining := make([]clickhouse.PartitionStat, 0, len(parts))
			for _, p := range parts {
				expired := (len(p.Partition) == 8 && p.Partition < cutoffDay) ||
					(len(p.Partition) == 6 && p.Partition < cutoffMonth)
				if expired {
					dCtx, dCancel := context.WithTimeout(ctx, 15*time.Second)
					err := r.ch.DropPartition(dCtx, p.Table, p.Partition)
					dCancel()
					if err != nil {
						log.Printf("[luma-eye] drop expired partition %s/%s: %v", p.Table, p.Partition, err)
						remaining = append(remaining, p)
						continue
					}
					chBytes -= int64(p.Bytes)
					if chBytes < 0 {
						chBytes = 0
					}
					dropped++
					continue
				}
				remaining = append(remaining, p)
			}

			if maxBytes > 0 && (chBytes+dataDirBytes) > maxBytes {
				lowWatermark := int64(float64(maxBytes) * 0.85)
				sort.Slice(remaining, func(i, j int) bool {
					di, dj := normalizePartitionDay(remaining[i].Partition), normalizePartitionDay(remaining[j].Partition)
					if di != dj {
						return di < dj
					}
					pi, pj := tableDropPriority(remaining[i].Table), tableDropPriority(remaining[j].Table)
					if pi != pj {
						return pi < pj
					}
					return remaining[i].Bytes > remaining[j].Bytes
				})
				for _, p := range remaining {
					if (chBytes + dataDirBytes) <= lowWatermark {
						break
					}
					dCtx, dCancel := context.WithTimeout(ctx, 15*time.Second)
					err := r.ch.DropPartition(dCtx, p.Table, p.Partition)
					dCancel()
					if err != nil {
						log.Printf("[luma-eye] drop oversized partition %s/%s: %v", p.Table, p.Partition, err)
						continue
					}
					chBytes -= int64(p.Bytes)
					if chBytes < 0 {
						chBytes = 0
					}
					dropped++
				}
			}
		}
	}

	r.mu.Lock()
	r.status.RetentionDays = retentionDays
	r.status.MaxStorageBytes = maxBytes
	r.status.DataDirBytes = dataDirBytes
	if chErr == nil {
		r.status.ClickHouseBytes = chBytes
		r.status.TotalBytes = chBytes + dataDirBytes
	} else if r.ch == nil {
		r.status.TotalBytes = dataDirBytes
	}
	r.status.DroppedPartitions += dropped
	r.status.PrunedSourceMaps += uint64(prunedMaps)
	r.status.LastRunAt = now
	r.mu.Unlock()

	if dropped > 0 || prunedMaps > 0 {
		log.Printf("[luma-eye] retention rotated dropped_partitions=%d pruned_sourcemaps=%d total_mb=%.1f",
			dropped, prunedMaps, float64(chBytes+dataDirBytes)/(1024*1024))
	}
	return chErr
}

func normalizePartitionDay(partition string) string {
	if len(partition) == 6 {
		return partition + "01"
	}
	return partition
}

func tableDropPriority(table string) int {
	switch table {
	case "replay_segments":
		return -1
	case "logs":
		return 0
	case "events":
		return 1
	case "vitals":
		return 2
	case "exceptions":
		return 3
	case "events_hourly":
		return 4
	default:
		return 5
	}
}

func measureDirBytes(root string) int64 {
	if root == "" {
		return 0
	}
	var total int64
	_ = filepath.WalkDir(root, func(_ string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		if info, err := d.Info(); err == nil {
			total += info.Size()
		}
		return nil
	})
	return total
}
