package pipeline

import (
	"crypto/sha1"
	"encoding/hex"
	"fmt"
	"regexp"
	"strings"

	"github.com/liutianjie/luma-eye/internal/model"
)

var (
	uuidRe   = regexp.MustCompile(`(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`)
	hexRe    = regexp.MustCompile(`(?i)\b0x[0-9a-f]+\b|\b[0-9a-f]{16,}\b`)
	urlRe    = regexp.MustCompile(`https?://[^\s'"]+`)
	numRe    = regexp.MustCompile(`\b\d+\b`)
	spacesRe = regexp.MustCompile(`\s+`)
)

func NormalizeMessage(msg string) string {
	s := strings.TrimSpace(msg)
	s = uuidRe.ReplaceAllString(s, "<uuid>")
	s = urlRe.ReplaceAllString(s, "<url>")
	s = hexRe.ReplaceAllString(s, "<hex>")
	s = numRe.ReplaceAllString(s, "<n>")
	s = spacesRe.ReplaceAllString(s, " ")
	if len(s) > 256 {
		s = s[:256]
	}
	return s
}

func ComputeFingerprint(errType, errMsg string, frames []model.StackFrame) string {
	var b strings.Builder
	b.WriteString(strings.TrimSpace(errType))
	b.WriteByte('|')

	// Collect up to 5 newest in-app frames; fall back to any frames if none are in-app.
	used := 0
	for i := len(frames) - 1; i >= 0 && used < 5; i-- {
		if !frames[i].InApp {
			continue
		}
		writeFrameKey(&b, frames[i])
		used++
	}
	if used == 0 {
		for i := len(frames) - 1; i >= 0 && used < 5; i-- {
			writeFrameKey(&b, frames[i])
			used++
		}
	}
	if used == 0 {
		b.WriteString(NormalizeMessage(errMsg))
	} else {
		// Also include normalized message prefix to distinguish distinct errors in same helper.
		b.WriteByte('|')
		b.WriteString(NormalizeMessage(errMsg))
	}

	sum := sha1.Sum([]byte(b.String()))
	return hex.EncodeToString(sum[:8])
}

func writeFrameKey(b *strings.Builder, f model.StackFrame) {
	file := f.Filename
	if idx := strings.IndexByte(file, '?'); idx >= 0 {
		file = file[:idx]
	}
	b.WriteString(file)
	b.WriteByte(':')
	b.WriteString(f.Function)
	if !f.Symbolicated && f.LineNo > 0 {
		fmt.Fprintf(b, ":%d", f.LineNo)
	} else if f.Symbolicated && f.LineNo > 0 {
		fmt.Fprintf(b, ":%d", f.LineNo)
	}
	b.WriteByte(';')
}
