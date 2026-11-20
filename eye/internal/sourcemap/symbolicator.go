package sourcemap

import (
	"container/list"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/go-sourcemap/sourcemap"
	"github.com/klauspost/compress/zstd"
	"github.com/liutianjie/luma-eye/internal/model"
)

type cachedLookup struct {
	key        string
	source     string
	function   string
	line       int
	col        int
	pre        []string
	ctxLine    string
	post       []string
	symbolized bool
}

type Symbolicator struct {
	baseDir string
	zstdEnc *zstd.Encoder
	zstdDec *zstd.Decoder

	lruMu   sync.Mutex
	lruCap  int
	lruList *list.List
	lruMap  map[string]*list.Element

	consumerMu sync.Mutex
	consumers  map[string]*sourcemap.Consumer
}

func NewSymbolicator(dataDir string, lruCapacity int) (*Symbolicator, error) {
	if lruCapacity <= 0 {
		lruCapacity = 4096
	}
	enc, err := zstd.NewWriter(nil, zstd.WithEncoderLevel(zstd.SpeedDefault))
	if err != nil {
		return nil, err
	}
	dec, err := zstd.NewReader(nil)
	if err != nil {
		return nil, err
	}
	baseDir := ""
	if dataDir != "" {
		baseDir = filepath.Join(dataDir, "sourcemaps")
		if err := os.MkdirAll(baseDir, 0o755); err != nil {
			return nil, fmt.Errorf("create sourcemaps dir: %w", err)
		}
	}
	return &Symbolicator{
		baseDir:   baseDir,
		zstdEnc:   enc,
		zstdDec:   dec,
		lruCap:    lruCapacity,
		lruList:   list.New(),
		lruMap:    make(map[string]*list.Element),
		consumers: make(map[string]*sourcemap.Consumer),
	}, nil
}

func (s *Symbolicator) Upload(project, release, assetPath string, rawMap []byte) (string, error) {
	if s.baseDir == "" {
		return "", fmt.Errorf("sourcemaps directory not configured")
	}
	var check struct {
		Version int `json:"version"`
	}
	if err := json.Unmarshal(rawMap, &check); err != nil || check.Version != 3 {
		return "", fmt.Errorf("invalid source map v3 JSON")
	}
	if _, err := sourcemap.Parse(assetPath, rawMap); err != nil {
		return "", fmt.Errorf("parse source map: %w", err)
	}

	norm := NormalizeAssetKey(assetPath)
	relDir := sanitizeSeg(release)
	if relDir == "" {
		relDir = "default"
	}
	projDir := sanitizeSeg(project)
	target := filepath.Join(s.baseDir, projDir, relDir, norm+".zst")
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return "", err
	}

	compressed := s.zstdEnc.EncodeAll(rawMap, make([]byte, 0, len(rawMap)/3))
	tmp := target + ".tmp"
	if err := os.WriteFile(tmp, compressed, 0o644); err != nil {
		return "", err
	}
	if err := os.Rename(tmp, target); err != nil {
		_ = os.Remove(tmp)
		return "", err
	}

	// Evict any previously cached consumer/entries for this release+asset.
	s.consumerMu.Lock()
	delete(s.consumers, target)
	s.consumerMu.Unlock()
	return norm, nil
}

func (s *Symbolicator) Symbolicate(project, release string, frames []model.StackFrame) ([]model.StackFrame, bool) {
	if s == nil || s.baseDir == "" || len(frames) == 0 {
		return frames, false
	}
	if release == "" {
		release = "default"
	}
	out := make([]model.StackFrame, len(frames))
	anyChanged := false

	for i, f := range frames {
		out[i] = f
		if f.LineNo <= 0 || f.Filename == "" {
			continue
		}
		// go-sourcemap expects 1-based line and 0-based generated column.
		col := f.ColNo - 1
		if col < 0 {
			col = 0
		}
		mapped, ok := s.lookup(project, release, f.Filename, f.LineNo, col)
		if !ok && release != "default" {
			mapped, ok = s.lookup(project, "default", f.Filename, f.LineNo, col)
		}
		if !ok || !mapped.symbolized {
			continue
		}
		if mapped.source != "" {
			out[i].Filename = mapped.source
		}
		if mapped.function != "" {
			out[i].Function = mapped.function
		} else if out[i].Function == "" {
			out[i].Function = "<anonymous>"
		}
		if mapped.line > 0 {
			out[i].LineNo = mapped.line
		}
		if mapped.col > 0 {
			out[i].ColNo = mapped.col
		}
		out[i].PreContext = mapped.pre
		out[i].ContextLine = mapped.ctxLine
		out[i].PostContext = mapped.post
		out[i].Symbolicated = true
		anyChanged = true
	}
	return out, anyChanged
}

func (s *Symbolicator) lookup(project, release, rawFile string, line, col int) (cachedLookup, bool) {
	assetNorm := NormalizeAssetKey(rawFile)
	cacheKey := fmt.Sprintf("%s|%s|%s|%d|%d", project, release, assetNorm, line, col)

	if v, ok := s.getLRU(cacheKey); ok {
		return v, v.symbolized
	}

	consumer := s.loadConsumer(project, release, assetNorm)
	if consumer == nil {
		// Also try matching by basename only (extract before '/' -> '__' normalization)
		// for iOS UUID sandbox paths (/private/var/.../main.jsbundle) or subpath deploys.
		if base := extractBaseAsset(rawFile); base != "" {
			baseNorm := NormalizeAssetKey(base)
			if baseNorm != assetNorm {
				consumer = s.loadConsumer(project, release, baseNorm)
			}
		}
	}
	if consumer == nil {
		return cachedLookup{}, false
	}

	src, name, origLine, origCol, ok := consumer.Source(line, col)
	if !ok {
		s.putLRU(cacheKey, cachedLookup{key: cacheKey, symbolized: false})
		return cachedLookup{}, false
	}

	pre, ctxLine, post := extractSourceContext(consumer.SourceContent(src), origLine, 4)
	res := cachedLookup{
		key:        cacheKey,
		source:     src,
		function:   name,
		line:       origLine,
		col:        origCol,
		pre:        pre,
		ctxLine:    ctxLine,
		post:       post,
		symbolized: true,
	}
	s.putLRU(cacheKey, res)
	return res, true
}

func (s *Symbolicator) loadConsumer(project, release, assetNorm string) *sourcemap.Consumer {
	target := filepath.Join(s.baseDir, sanitizeSeg(project), sanitizeSeg(release), assetNorm+".zst")
	s.consumerMu.Lock()
	if c, ok := s.consumers[target]; ok {
		s.consumerMu.Unlock()
		return c
	}
	s.consumerMu.Unlock()

	compressed, err := os.ReadFile(target)
	if err != nil {
		return nil
	}
	raw, err := s.zstdDec.DecodeAll(compressed, nil)
	if err != nil {
		return nil
	}
	c, err := sourcemap.Parse(assetNorm, raw)
	if err != nil {
		return nil
	}

	s.consumerMu.Lock()
	if len(s.consumers) > 128 {
		// Simple bound on loaded consumer objects in memory.
		for k := range s.consumers {
			delete(s.consumers, k)
			if len(s.consumers) <= 64 {
				break
			}
		}
	}
	s.consumers[target] = c
	s.consumerMu.Unlock()
	return c
}

func (s *Symbolicator) getLRU(key string) (cachedLookup, bool) {
	s.lruMu.Lock()
	defer s.lruMu.Unlock()
	el, ok := s.lruMap[key]
	if !ok {
		return cachedLookup{}, false
	}
	s.lruList.MoveToFront(el)
	return el.Value.(cachedLookup), true
}

func (s *Symbolicator) putLRU(key string, val cachedLookup) {
	s.lruMu.Lock()
	defer s.lruMu.Unlock()
	if el, ok := s.lruMap[key]; ok {
		el.Value = val
		s.lruList.MoveToFront(el)
		return
	}
	el := s.lruList.PushFront(val)
	s.lruMap[key] = el
	for s.lruList.Len() > s.lruCap {
		tail := s.lruList.Back()
		if tail == nil {
			break
		}
		old := s.lruList.Remove(tail).(cachedLookup)
		delete(s.lruMap, old.key)
	}
}

func extractBaseAsset(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	if u, err := url.Parse(raw); err == nil && u.Path != "" {
		raw = u.Path
	} else if idx := strings.IndexByte(raw, '?'); idx >= 0 {
		raw = raw[:idx]
	}
	raw = strings.ReplaceAll(raw, "\\", "/")
	base := path.Base(raw)
	if base == "." || base == "/" {
		return ""
	}
	return base
}

func NormalizeAssetKey(raw string) string {
	raw = strings.TrimSpace(raw)
	if u, err := url.Parse(raw); err == nil && u.Path != "" {
		raw = u.Path
	} else if idx := strings.IndexByte(raw, '?'); idx >= 0 {
		raw = raw[:idx]
	}
	raw = strings.TrimPrefix(raw, "~/")
	raw = strings.TrimPrefix(raw, "./")
	raw = strings.TrimPrefix(raw, "/")
	raw = strings.TrimSuffix(raw, ".map")
	// Replace directory separators safely so path traversal is impossible.
	raw = strings.ReplaceAll(raw, "..", "_")
	raw = strings.ReplaceAll(raw, "\\", "__")
	raw = strings.ReplaceAll(raw, "/", "__")
	if raw == "" {
		return "bundle.js"
	}
	return raw
}

func sanitizeSeg(s string) string {
	s = strings.TrimSpace(s)
	s = strings.ReplaceAll(s, "..", "_")
	s = strings.ReplaceAll(s, "/", "_")
	s = strings.ReplaceAll(s, "\\", "_")
	if s == "" {
		return "default"
	}
	return s
}

func extractSourceContext(content string, targetLine, radius int) (pre []string, ctxLine string, post []string) {
	if content == "" || targetLine <= 0 {
		return nil, "", nil
	}
	lines := strings.Split(strings.ReplaceAll(content, "\r\n", "\n"), "\n")
	idx := targetLine - 1
	if idx < 0 || idx >= len(lines) {
		return nil, "", nil
	}
	ctxLine = lines[idx]
	start := idx - radius
	if start < 0 {
		start = 0
	}
	for i := start; i < idx; i++ {
		pre = append(pre, lines[i])
	}
	end := idx + radius
	if end >= len(lines) {
		end = len(lines) - 1
	}
	for i := idx + 1; i <= end; i++ {
		post = append(post, lines[i])
	}
	return pre, ctxLine, post
}

type sourceMapFile struct {
	path    string
	size    int64
	modTime time.Time
}

// Prune removes expired .zst source maps (while preserving the newest
// keepReleasesPerProject releases per project from age deletion) and enforces
// an overall byte budget across baseDir by deleting oldest files first.
func (s *Symbolicator) Prune(maxAge time.Duration, maxTotalBytes int64, keepReleasesPerProject int) (int, int64, error) {
	if s == nil || s.baseDir == "" {
		return 0, 0, nil
	}
	projEntries, err := os.ReadDir(s.baseDir)
	if err != nil {
		if os.IsNotExist(err) {
			return 0, 0, nil
		}
		return 0, 0, err
	}

	cutoff := time.Now().Add(-maxAge)
	var kept []sourceMapFile
	var removedPaths []string
	var totalBytes int64

	for _, proj := range projEntries {
		if !proj.IsDir() {
			continue
		}
		projDir := filepath.Join(s.baseDir, proj.Name())
		relEntries, err := os.ReadDir(projDir)
		if err != nil {
			continue
		}

		type relGroup struct {
			dir     string
			newest  time.Time
			files   []sourceMapFile
		}
		groups := make([]relGroup, 0, len(relEntries))
		for _, rel := range relEntries {
			if !rel.IsDir() {
				continue
			}
			relDir := filepath.Join(projDir, rel.Name())
			files, newest := scanReleaseSourceMaps(relDir)
			if len(files) == 0 {
				_ = os.RemoveAll(relDir)
				continue
			}
			groups = append(groups, relGroup{dir: relDir, newest: newest, files: files})
		}

		sort.Slice(groups, func(i, j int) bool {
			return groups[i].newest.After(groups[j].newest)
		})

		for idx, g := range groups {
			protected := keepReleasesPerProject > 0 && idx < keepReleasesPerProject
			if !protected && maxAge > 0 && g.newest.Before(cutoff) {
				for _, f := range g.files {
					if err := os.Remove(f.path); err == nil || os.IsNotExist(err) {
						removedPaths = append(removedPaths, f.path)
					}
				}
				_ = os.RemoveAll(g.dir)
				continue
			}
			for _, f := range g.files {
				kept = append(kept, f)
				totalBytes += f.size
			}
		}
	}

	if maxTotalBytes > 0 && totalBytes > maxTotalBytes {
		lowWatermark := int64(float64(maxTotalBytes) * 0.85)
		sort.Slice(kept, func(i, j int) bool {
			return kept[i].modTime.Before(kept[j].modTime)
		})
		for _, f := range kept {
			if totalBytes <= lowWatermark {
				break
			}
			if err := os.Remove(f.path); err == nil || os.IsNotExist(err) {
				removedPaths = append(removedPaths, f.path)
				totalBytes -= f.size
				_ = os.Remove(filepath.Dir(f.path))
			}
		}
	}

	if len(removedPaths) > 0 {
		s.consumerMu.Lock()
		for _, p := range removedPaths {
			delete(s.consumers, p)
		}
		s.consumerMu.Unlock()

		s.lruMu.Lock()
		s.lruList.Init()
		s.lruMap = make(map[string]*list.Element)
		s.lruMu.Unlock()
	}

	return len(removedPaths), totalBytes, nil
}

func scanReleaseSourceMaps(relDir string) ([]sourceMapFile, time.Time) {
	var files []sourceMapFile
	var newest time.Time
	_ = filepath.WalkDir(relDir, func(p string, d os.DirEntry, wErr error) error {
		if wErr != nil || d.IsDir() {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return nil
		}
		if info.ModTime().After(newest) {
			newest = info.ModTime()
		}
		files = append(files, sourceMapFile{
			path:    p,
			size:    info.Size(),
			modTime: info.ModTime(),
		})
		return nil
	})
	return files, newest
}
