package pipeline

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/klauspost/compress/zstd"
	"github.com/liutianjie/luma-eye/internal/alert"
	"github.com/liutianjie/luma-eye/internal/clickhouse"
	"github.com/liutianjie/luma-eye/internal/config"
	"github.com/liutianjie/luma-eye/internal/model"
	"github.com/liutianjie/luma-eye/internal/observe"
	"github.com/liutianjie/luma-eye/internal/project"
	"github.com/liutianjie/luma-eye/internal/ratelimit"
	"github.com/liutianjie/luma-eye/internal/sourcemap"
)

type queueItem struct {
	events         []model.Event
	exceptions     []model.ExceptionEvent
	logs           []model.LogRecord
	vitals         []model.VitalRecord
	replaySegments []model.ReplaySegment
}

type Batcher struct {
	cfg          *config.Config
	store        *project.Store
	ch           *clickhouse.Client
	symbolicator *sourcemap.Symbolicator
	storm        *ratelimit.StormSampler
	notifier     *alert.FeishuNotifier
	metrics      *observe.Metrics

	queue    chan queueItem
	zstdEnc  *zstd.Encoder
	httpCli  *http.Client
	stopOnce sync.Once
	doneCh   chan struct{}
}

func NewBatcher(
	cfg *config.Config,
	store *project.Store,
	ch *clickhouse.Client,
	symbolicator *sourcemap.Symbolicator,
	notifier *alert.FeishuNotifier,
	metrics *observe.Metrics,
) (*Batcher, error) {
	enc, err := zstd.NewWriter(nil, zstd.WithEncoderLevel(zstd.SpeedDefault))
	if err != nil {
		return nil, err
	}
	qCap := cfg.QueueCapacity
	if qCap <= 0 {
		qCap = 32768
	}
	return &Batcher{
		cfg:          cfg,
		store:        store,
		ch:           ch,
		symbolicator: symbolicator,
		storm:        ratelimit.NewStormSampler(time.Minute, 10),
		notifier:     notifier,
		metrics:      metrics,
		queue:        make(chan queueItem, qCap),
		zstdEnc:      enc,
		httpCli:      &http.Client{Timeout: 15 * time.Second},
		doneCh:       make(chan struct{}),
	}, nil
}

func (b *Batcher) Start(ctx context.Context) {
	interval := b.cfg.BatchInterval
	if interval <= 0 {
		interval = 2 * time.Second
	}
	maxBatch := b.cfg.BatchSize
	if maxBatch <= 0 {
		maxBatch = 2000
	}

	if b.cfg.Mode == config.ModeEdge {
		go b.syncProjectsLoop(ctx)
	}

	go func() {
		defer close(b.doneCh)
		ticker := time.NewTicker(interval)
		defer ticker.Stop()

		var pending model.RelayBatch
		flush := func(flushCtx context.Context) {
			if pending.Len() == 0 && b.cfg.Mode == config.ModeEdge {
				return
			}
			b.flushBatch(flushCtx, &pending)
			pending = model.RelayBatch{}
		}

		for {
			select {
			case <-ctx.Done():
				// Drain remaining items in channel without blocking
				for {
					select {
					case item := <-b.queue:
						appendItem(&pending, item)
					default:
						drainCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
						flush(drainCtx)
						cancel()
						return
					}
				}
			case item := <-b.queue:
				appendItem(&pending, item)
				if pending.Len() >= maxBatch {
					flush(ctx)
				}
			case <-ticker.C:
				flush(ctx)
			}
		}
	}()
}

func (b *Batcher) Shutdown(ctx context.Context) {
	select {
	case <-b.doneCh:
	case <-ctx.Done():
	}
}

func (b *Batcher) Enqueue(batch model.RelayBatch) bool {
	if batch.Len() == 0 {
		return true
	}
	select {
	case b.queue <- queueItem{
		events:         batch.Events,
		exceptions:     batch.Exceptions,
		logs:           batch.Logs,
		vitals:         batch.Vitals,
		replaySegments: batch.ReplaySegments,
	}:
		return true
	default:
		b.metrics.AddDropped(uint64(batch.Len()))
		return false
	}
}

func appendItem(dst *model.RelayBatch, item queueItem) {
	dst.Events = append(dst.Events, item.events...)
	dst.Exceptions = append(dst.Exceptions, item.exceptions...)
	dst.Logs = append(dst.Logs, item.logs...)
	dst.Vitals = append(dst.Vitals, item.vitals...)
	dst.ReplaySegments = append(dst.ReplaySegments, item.replaySegments...)
}

func (b *Batcher) flushBatch(ctx context.Context, batch *model.RelayBatch) {
	if b.cfg.Mode == config.ModeEdge {
		if batch.Len() == 0 {
			return
		}
		if err := b.relayToCore(ctx, *batch); err != nil {
			log.Printf("[luma-eye] edge relay failed (%d items): %v", batch.Len(), err)
			b.metrics.AddDropped(uint64(batch.Len()))
		}
		return
	}

	// Core or All mode: process exceptions (symbolicate, re-fingerprint, storm collapse, alerts),
	// update metrics, and bulk-insert into ClickHouse.
	now := time.Now().UTC()

	var admittedExceptions []model.ExceptionEvent
	for _, exc := range batch.Exceptions {
		if b.symbolicator != nil && len(exc.Frames) > 0 {
			if symFrames, changed := b.symbolicator.Symbolicate(exc.Project, exc.Release, exc.Frames); changed {
				exc.Frames = symFrames
				exc.Culprit = ComputeCulprit(symFrames)
				exc.Fingerprint = ComputeFingerprint(exc.ErrorType, exc.ErrorMessage, symFrames)
				exc.FramesJSON = ""
			}
		}
		exc.EncodeJSONFields()

		if b.storm.Admit(exc.Project, exc.Fingerprint, now, exc) {
			admittedExceptions = append(admittedExceptions, exc)
			if b.store != nil {
				if shouldAlert, reason, _ := b.store.CheckAndRecordIssue(exc.Project, exc.Fingerprint, now); shouldAlert {
					if b.notifier != nil {
						b.notifier.NotifyIssueAsync(reason, exc)
					}
				}
			}
		}
	}

	// Drain collapsed error storm weights so total_count remains accurate in issue_rollups.
	for _, collapsed := range b.storm.DrainCollapsed(now) {
		if sample, ok := collapsed.Sample.(model.ExceptionEvent); ok {
			sample.TS = model.FormatTS(now)
			sample.OccurrenceWeight = collapsed.Count
			sample.EncodeJSONFields()
			admittedExceptions = append(admittedExceptions, sample)
		}
	}

	// Update metrics counters
	evTotals := make(map[string][2]uint64)
	for _, ev := range batch.Events {
		c := evTotals[ev.Project]
		c[0]++
		if ev.EventType == "pageview" {
			c[1]++
		}
		evTotals[ev.Project] = c
	}
	for proj, counts := range evTotals {
		b.metrics.RecordEvents(proj, counts[0], counts[1])
	}
	excTotals := make(map[string]uint64)
	for _, exc := range admittedExceptions {
		w := uint64(exc.OccurrenceWeight)
		if w == 0 {
			w = 1
		}
		excTotals[exc.Project] += w
	}
	for proj, cnt := range excTotals {
		b.metrics.RecordExceptions(proj, cnt)
	}
	logTotals := make(map[string]uint64)
	for _, lg := range batch.Logs {
		logTotals[lg.Project]++
	}
	for proj, cnt := range logTotals {
		b.metrics.RecordLogs(proj, cnt)
	}
	vitalTotals := make(map[string]uint64)
	for _, vt := range batch.Vitals {
		vitalTotals[vt.Project]++
	}
	for proj, cnt := range vitalTotals {
		b.metrics.RecordVitals(proj, cnt)
	}

	if b.ch == nil {
		return
	}

	if len(batch.Events) > 0 {
		b.insertWithRetry(ctx, "events", batch.Events)
	}
	if len(admittedExceptions) > 0 {
		b.insertWithRetry(ctx, "exceptions", admittedExceptions)
	}
	if len(batch.Logs) > 0 {
		b.insertWithRetry(ctx, "logs", batch.Logs)
	}
	if len(batch.Vitals) > 0 {
		b.insertWithRetry(ctx, "vitals", batch.Vitals)
	}
	if len(batch.ReplaySegments) > 0 {
		b.insertWithRetry(ctx, "replay_segments", batch.ReplaySegments)
	}
}

func (b *Batcher) insertWithRetry(ctx context.Context, table string, rows any) {
	var err error
	for attempt := 0; attempt < 3; attempt++ {
		if err = b.ch.InsertJSONEachRow(ctx, table, rows); err == nil {
			return
		}
		if ctx.Err() != nil {
			break
		}
		time.Sleep(time.Duration(attempt+1) * 250 * time.Millisecond)
	}
	b.metrics.IncClickHouseError()
	log.Printf("[luma-eye] clickhouse insert %s failed: %v", table, err)
}

func (b *Batcher) relayToCore(ctx context.Context, batch model.RelayBatch) error {
	raw, err := json.Marshal(batch)
	if err != nil {
		return err
	}
	compressed := b.zstdEnc.EncodeAll(raw, make([]byte, 0, len(raw)/4))

	target := strings.TrimRight(b.cfg.CoreURL, "/") + "/internal/v1/relay"
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, target, bytes.NewReader(compressed))
		if err != nil {
			return err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Content-Encoding", "zstd")
		req.Header.Set("Authorization", "Bearer "+b.cfg.RelaySecret)

		resp, err := b.httpCli.Do(req)
		if err == nil {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if resp.StatusCode < 300 {
				b.metrics.AddRelayBytes(uint64(len(raw)), uint64(len(compressed)))
				return nil
			}
			lastErr = fmt.Errorf("core relay status %d", resp.StatusCode)
		} else {
			lastErr = err
		}
		time.Sleep(time.Duration(attempt+1) * 200 * time.Millisecond)
	}
	return lastErr
}

func (b *Batcher) syncProjectsLoop(ctx context.Context) {
	syncOnce := func() {
		reqCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		target := strings.TrimRight(b.cfg.CoreURL, "/") + "/internal/v1/projects-sync"
		req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, target, nil)
		if err != nil {
			return
		}
		req.Header.Set("Authorization", "Bearer "+b.cfg.RelaySecret)
		resp, err := b.httpCli.Do(req)
		if err != nil {
			return
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return
		}
		var payload struct {
			Projects []*model.Project `json:"projects"`
		}
		if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&payload); err == nil && len(payload.Projects) > 0 {
			_ = b.store.ReplaceAllProjects(payload.Projects)
		}
	}

	syncOnce()
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			syncOnce()
		}
	}
}
