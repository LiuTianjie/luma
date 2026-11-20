package pipeline

import (
	"regexp"
	"strconv"
	"strings"

	"github.com/liutianjie/luma-eye/internal/model"
)

var (
	// V8 / Chrome / Node: "    at fn (https://host/assets/index.js:1:234)" or "    at https://host/assets/index.js:1:234"
	v8WithFnRe = regexp.MustCompile(`^\s*at\s+(?:async\s+)?(.+?)\s+\((.+?):(\d+):(\d+)\)\s*$`)
	v8AnonRe   = regexp.MustCompile(`^\s*at\s+(?:async\s+)?(.+?):(\d+):(\d+)\s*$`)
	// Firefox / Safari: "fn@https://host/assets/index.js:1:234" or "@https://host/assets/index.js:1:234"
	geckoRe = regexp.MustCompile(`^\s*(.*?)@(.+?):(\d+):(\d+)\s*$`)
)

// ParseStackString parses JS (V8 / Firefox / Safari) or React component stacks into frames ordered oldest-to-newest.
func ParseStackString(stack string) []model.StackFrame {
	if strings.TrimSpace(stack) == "" {
		return nil
	}
	lines := strings.Split(stack, "\n")
	parsed := make([]model.StackFrame, 0, len(lines))
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if line == "at native" || strings.HasSuffix(line, "(native)") || strings.HasSuffix(line, "([native code])") {
			continue
		}
		if m := v8WithFnRe.FindStringSubmatch(line); len(m) == 5 {
			ln, _ := strconv.Atoi(m[3])
			cn, _ := strconv.Atoi(m[4])
			rawFile, cn := normalizeHermesLocation(m[2], cn)
			parsed = append(parsed, makeFrame(rawFile, m[1], ln, cn))
			continue
		}
		if m := v8AnonRe.FindStringSubmatch(line); len(m) == 4 {
			ln, _ := strconv.Atoi(m[2])
			cn, _ := strconv.Atoi(m[3])
			rawFile, cn := normalizeHermesLocation(m[1], cn)
			parsed = append(parsed, makeFrame(rawFile, "<anonymous>", ln, cn))
			continue
		}
		if m := geckoRe.FindStringSubmatch(line); len(m) == 5 {
			ln, _ := strconv.Atoi(m[3])
			cn, _ := strconv.Atoi(m[4])
			fn := strings.TrimSpace(m[1])
			if fn == "" {
				fn = "<anonymous>"
			}
			parsed = append(parsed, makeFrame(m[2], fn, ln, cn))
			continue
		}
	}
	if len(parsed) <= 1 {
		return parsed
	}
	// Browser stack strings are newest-first; reverse to Sentry-compatible oldest-first.
	for i, j := 0, len(parsed)-1; i < j; i, j = i+1, j-1 {
		parsed[i], parsed[j] = parsed[j], parsed[i]
	}
	return parsed
}

// normalizeHermesLocation strips Hermes's "address at " prefix and converts 0-based
// bytecode column offsets to the 1-based ColNo convention used across stack frames.
func normalizeHermesLocation(rawFile string, col int) (string, int) {
	trimmed := strings.TrimSpace(rawFile)
	if len(trimmed) > 11 && strings.EqualFold(trimmed[:11], "address at ") {
		return strings.TrimSpace(trimmed[11:]), col + 1
	}
	return trimmed, col
}

func makeFrame(rawFile, fn string, line, col int) model.StackFrame {
	rawFile = strings.TrimSpace(rawFile)
	fn = strings.TrimSpace(fn)
	if fn == "" {
		fn = "<anonymous>"
	}
	return model.StackFrame{
		Filename: rawFile,
		AbsPath:  rawFile,
		Function: fn,
		LineNo:   line,
		ColNo:    col,
		InApp:    IsInAppFile(rawFile),
	}
}

func IsInAppFile(file string) bool {
	if file == "" {
		return false
	}
	low := strings.ToLower(strings.TrimSpace(file))
	if low == "native" || low == "[native code]" ||
		strings.Contains(low, "node_modules") ||
		strings.Contains(low, "internalbytecode.js") ||
		strings.HasPrefix(low, "chrome-extension://") ||
		strings.HasPrefix(low, "moz-extension://") ||
		strings.HasPrefix(low, "safari-extension://") ||
		strings.HasPrefix(low, "node:") ||
		strings.Contains(low, "/<anonymous>") {
		return false
	}
	return true
}

// ComputeCulprit returns "file:line in function" from the newest in-app frame (or newest frame).
func ComputeCulprit(frames []model.StackFrame) string {
	if len(frames) == 0 {
		return ""
	}
	for i := len(frames) - 1; i >= 0; i-- {
		f := frames[i]
		if f.InApp {
			return formatCulprit(f)
		}
	}
	return formatCulprit(frames[len(frames)-1])
}

func formatCulprit(f model.StackFrame) string {
	name := f.Filename
	if idx := strings.LastIndexByte(name, '/'); idx >= 0 && idx+1 < len(name) {
		name = name[idx+1:]
	}
	if f.LineNo > 0 && f.Function != "" && f.Function != "<anonymous>" {
		return name + ":" + strconv.Itoa(f.LineNo) + " in " + f.Function
	}
	if f.LineNo > 0 {
		return name + ":" + strconv.Itoa(f.LineNo)
	}
	return name
}
