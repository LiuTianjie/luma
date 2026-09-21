// Package envfile parses local configuration without evaluating or exporting it.
package envfile

import (
	"fmt"
	"regexp"
	"strings"
)

const MaxBytes = 4 << 20

var namePattern = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
var referencePattern = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)\}`)

func Parse(data []byte) (map[string]string, error) {
	if len(data) > MaxBytes {
		return nil, fmt.Errorf("env file exceeds size limit")
	}
	values := map[string]string{}
	for i, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimSpace(strings.TrimPrefix(line, "export "))
		key, value, ok := strings.Cut(line, "=")
		key = strings.TrimSpace(key)
		if !ok || !namePattern.MatchString(key) {
			return nil, fmt.Errorf("invalid env file line %d: expected KEY=VALUE", i+1)
		}
		value = strings.TrimSpace(value)
		if strings.HasPrefix(value, "'") || strings.HasPrefix(value, `"`) {
			parsed, err := quoted(value)
			if err != nil {
				return nil, fmt.Errorf("invalid quoted env value at line %d", i+1)
			}
			value = parsed
		} else {
			value, _, _ = strings.Cut(value, " #")
			value = strings.TrimSpace(value)
		}
		values[key] = value
	}
	return values, nil
}

// Python uses POSIX shlex and takes the first word. Parse the complete line so
// trailing unmatched quotes still fail, without executing substitutions.
func quoted(value string) (string, error) {
	var first, word strings.Builder
	quote := byte(0)
	started, finished := false, false
	for i := 0; i < len(value); i++ {
		ch := value[i]
		if ch == '\\' && quote != '\'' {
			if i+1 == len(value) {
				return "", fmt.Errorf("unfinished escape")
			}
			next := value[i+1]
			if quote != '"' || next == '"' || next == '\\' {
				i++
				ch = next
			}
			word.WriteByte(ch)
			started = true
			continue
		}
		if quote != 0 {
			if ch == quote {
				quote = 0
			} else {
				word.WriteByte(ch)
			}
			continue
		}
		if ch == '\'' || ch == '"' {
			quote = ch
			started = true
			continue
		}
		if ch == ' ' || ch == '\t' || ch == '\r' {
			if started && !finished {
				first.WriteString(word.String())
				finished = true
			}
			word.Reset()
			started = false
			continue
		}
		word.WriteByte(ch)
		started = true
	}
	if quote != 0 {
		return "", fmt.Errorf("unfinished quote")
	}
	if !finished {
		first.WriteString(word.String())
	}
	return first.String(), nil
}

func Referenced(values map[string]string, texts ...string) map[string]string {
	result := map[string]string{}
	for _, text := range texts {
		for _, match := range referencePattern.FindAllStringSubmatch(text, -1) {
			if value, ok := values[match[1]]; ok {
				result[match[1]] = value
			}
		}
	}
	return result
}
