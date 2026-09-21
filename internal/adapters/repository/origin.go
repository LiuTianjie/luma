// Package repository reads local checkout metadata without contacting a remote.
package repository

import (
	"context"
	"net/url"
	"os/exec"
	"strings"
	"time"
)

func Origin(directory string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	data, err := exec.CommandContext(ctx, "git", "-C", directory, "config", "--get", "remote.origin.url").Output()
	if err != nil || len(data) > 4096 {
		return ""
	}
	text := strings.TrimSpace(string(data))
	if strings.Contains(text, "://") {
		parsed, err := url.Parse(text)
		if err != nil {
			return ""
		}
		parsed.User = nil
		parsed.RawQuery = ""
		parsed.Fragment = ""
		return parsed.String()
	}
	return text
}
