package retention

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"github.com/liutianjie/luma-eye/internal/clickhouse"
	"github.com/liutianjie/luma-eye/internal/config"
	"github.com/liutianjie/luma-eye/internal/sourcemap"
)

type fakeBackend struct {
	totalBytes int64
	parts      []clickhouse.PartitionStat
	dropped    []string
}

func (f *fakeBackend) StorageSnapshot(_ context.Context) (int64, []clickhouse.PartitionStat, error) {
	out := make([]clickhouse.PartitionStat, len(f.parts))
	copy(out, f.parts)
	return f.totalBytes, out, nil
}

func (f *fakeBackend) DropPartition(_ context.Context, table, partition string) error {
	f.dropped = append(f.dropped, table+"/"+partition)
	return nil
}

func TestReaperAgeRotationAndSourceMapProtection(t *testing.T) {
	dir := t.TempDir()
	sym, err := sourcemap.NewSymbolicator(dir, 16)
	if err != nil {
		t.Fatal(err)
	}
	rawMap := []byte(`{"version":3,"sources":["src/app.ts"],"names":["main"],"mappings":"AAAA"}`)
	oldTime := time.Now().Add(-15 * 24 * time.Hour)
	for _, rel := range []string{"r1", "r2", "r3", "r4"} {
		if _, err := sym.Upload("demo", rel, "app.js", rawMap); err != nil {
			t.Fatal(err)
		}
	}
	// Make all releases older than 7 days, staggered chronologically (r1 oldest, r4 newest among old).
	for i, rel := range []string{"r1", "r2", "r3", "r4"} {
		mt := oldTime.Add(time.Duration(i) * time.Hour)
		p := filepath.Join(dir, "sourcemaps", "demo", rel, "app.js.zst")
		if err := os.Chtimes(p, mt, mt); err != nil {
			t.Fatal(err)
		}
	}

	now := time.Date(2026, 11, 18, 12, 0, 0, 0, time.UTC)
	backend := &fakeBackend{
		totalBytes: 500,
		parts: []clickhouse.PartitionStat{
			{Table: "events", Partition: "20261101", Bytes: 200},
			{Table: "logs", Partition: "20261110", Bytes: 100},   // 8 days before 20261118 -> expired (< 20261111)
			{Table: "logs", Partition: "20261111", Bytes: 100},   // exactly cutoff -> keep
			{Table: "events", Partition: "20261118", Bytes: 100}, // today -> keep
		},
	}

	cfg := &config.Config{
		DataDir:           dir,
		RetentionDays:     7,
		MaxStorageBytes:   15 << 30,
		RetentionInterval: time.Minute,
	}
	r := NewReaper(cfg, backend, nil, sym)
	if err := r.RunOnce(context.Background(), now); err != nil {
		t.Fatal(err)
	}

	wantDropped := []string{"events/20261101", "logs/20261110"}
	if !reflect.DeepEqual(backend.dropped, wantDropped) {
		t.Fatalf("dropped partitions = %v, want %v", backend.dropped, wantDropped)
	}

	// r1 (oldest beyond keepReleasesPerProject=3) removed; r2..r4 preserved.
	if _, err := os.Stat(filepath.Join(dir, "sourcemaps", "demo", "r1")); !os.IsNotExist(err) {
		t.Fatalf("expected oldest release r1 to be pruned, stat err=%v", err)
	}
	for _, rel := range []string{"r2", "r3", "r4"} {
		if _, err := os.Stat(filepath.Join(dir, "sourcemaps", "demo", rel, "app.js.zst")); err != nil {
			t.Fatalf("expected protected release %s to remain: %v", rel, err)
		}
	}
}

func TestReaperSizeCapPriorityAndWatermark(t *testing.T) {
	now := time.Date(2026, 11, 18, 12, 0, 0, 0, time.UTC)
	// Max = 1000 bytes, low watermark = 850 bytes. All partitions within 7d window.
	backend := &fakeBackend{
		totalBytes: 1200,
		parts: []clickhouse.PartitionStat{
			{Table: "events", Partition: "20261115", Bytes: 250},
			{Table: "logs", Partition: "20261115", Bytes: 200}, // same oldest day -> logs dropped before events!
			{Table: "events", Partition: "20261118", Bytes: 750},
		},
	}
	cfg := &config.Config{
		RetentionDays:   7,
		MaxStorageBytes: 1000,
	}
	r := NewReaper(cfg, backend, nil, nil)
	if err := r.RunOnce(context.Background(), now); err != nil {
		t.Fatal(err)
	}

	// Dropping logs/20261115 (-200 -> 1000 > 850) then events/20261115 (-250 -> 750 <= 850) stops before today's partition.
	wantDropped := []string{"logs/20261115", "events/20261115"}
	if !reflect.DeepEqual(backend.dropped, wantDropped) {
		t.Fatalf("dropped partitions = %v, want %v", backend.dropped, wantDropped)
	}
	st := r.Snapshot()
	if st.TotalBytes != 750 || st.DroppedPartitions != 2 {
		t.Fatalf("unexpected status snapshot: %+v", st)
	}
}

func TestReaperNeverDropsActiveTodayOrCurrentMonthPartition(t *testing.T) {
	now := time.Date(2026, 11, 18, 12, 0, 0, 0, time.UTC)
	backend := &fakeBackend{
		totalBytes: 1500,
		parts: []clickhouse.PartitionStat{
			{Table: "events", Partition: "20261115", Bytes: 500},
			{Table: "events", Partition: "20261118", Bytes: 500},        // today's active day partition
			{Table: "events_hourly", Partition: "202611", Bytes: 500},   // current active month partition
		},
	}
	cfg := &config.Config{
		RetentionDays:   7,
		MaxStorageBytes: 100, // far below remaining 1000B so reaper wants to drop everything it can
	}
	r := NewReaper(cfg, backend, nil, nil)
	if err := r.RunOnce(context.Background(), now); err != nil {
		t.Fatal(err)
	}

	wantDropped := []string{"events/20261115"}
	if !reflect.DeepEqual(backend.dropped, wantDropped) {
		t.Fatalf("expected only older partition dropped, got %v (want %v)", backend.dropped, wantDropped)
	}
}
