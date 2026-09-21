// Package registry owns image identity and allowed internal cache destinations.
// It does not execute tools, access credentials or make network requests.
package registry

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
)

var referencePattern = regexp.MustCompile(`^[A-Za-z0-9\[][A-Za-z0-9._:/@+\[\]-]*$`)
var tagPattern = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$`)
var digestPattern = regexp.MustCompile(`^sha256:[a-f0-9]{64}$`)

type Image struct{ Reference, Repository, Registry, Path, Tag, Digest string }

func ParseImage(value string) (Image, error) {
	value = strings.TrimSpace(value)
	if len(value) > 1024 || !referencePattern.MatchString(value) || strings.Contains(value, "://") || strings.Contains(value, "//") {
		return Image{}, fmt.Errorf("invalid Docker image reference")
	}
	i := Image{Reference: value}
	repo := value
	if head, digest, ok := strings.Cut(repo, "@"); ok {
		if !ValidDigest(digest) {
			return Image{}, fmt.Errorf("invalid image digest")
		}
		i.Digest, repo = digest, head
	}
	if colon := strings.LastIndex(repo, ":"); colon > strings.LastIndex(repo, "/") {
		i.Tag, repo = repo[colon+1:], repo[:colon]
		if !tagPattern.MatchString(i.Tag) {
			return Image{}, fmt.Errorf("invalid image tag")
		}
	}
	i.Repository, i.Path, i.Registry = repo, repo, "registry-1.docker.io"
	if host, path, ok := strings.Cut(repo, "/"); ok && (host == "localhost" || strings.ContainsAny(host, ".:")) {
		i.Registry, i.Path = host, path
	}
	for _, part := range strings.Split(i.Path, "/") {
		if part == "" || part == "." || part == ".." || strings.ContainsAny(part, ":@[]+") {
			return Image{}, fmt.Errorf("invalid image repository")
		}
	}
	return i, nil
}
func ValidDigest(value string) bool { return digestPattern.MatchString(value) }

func MutableLatest(image Image) bool {
	return image.Digest == "" && (image.Tag == "" || image.Tag == "latest")
}

func SameRepository(a, b Image) bool {
	canonical := func(i Image) string {
		host, path := strings.ToLower(i.Registry), i.Path
		if host == "docker.io" {
			host = "registry-1.docker.io"
		}
		if host == "registry-1.docker.io" && !strings.Contains(path, "/") {
			path = "library/" + path
		}
		return host + "/" + path
	}
	return canonical(a) == canonical(b)
}

// ResolveResult validates an Agent's chosen reference against the requested
// repository. A pinned digest cannot silently change to a different image.
func ResolveResult(requested, deployed string) (string, error) {
	want, err := ParseImage(requested)
	if err != nil {
		return "", err
	}
	got, err := ParseImage(deployed)
	if err != nil || !SameRepository(want, got) || want.Digest != "" && want.Digest != got.Digest {
		return "", fmt.Errorf("image resolution returned an unrelated image")
	}
	if got.Digest != "" {
		return want.Repository + "@" + got.Digest, nil
	}
	if deployed != want.Reference {
		return "", fmt.Errorf("image resolution returned an unverified tag")
	}
	return want.Reference, nil
}

// RepoDigest preserves the requested repository when Docker lists more than one
// alias. Malformed tool output must never turn into a trusted deployment image.
func RepoDigest(image Image, output string) string {
	lines := strings.Split(strings.TrimSpace(output), "\n")
	var values []string
	if json.Unmarshal([]byte(lines[len(lines)-1]), &values) != nil {
		return ""
	}
	var fallback string
	for _, value := range values {
		parsed, err := ParseImage(value)
		if err != nil || parsed.Digest == "" {
			continue
		}
		if SameRepository(parsed, image) {
			return image.Repository + "@" + parsed.Digest
		}
		if fallback == "" {
			fallback = image.Repository + "@" + parsed.Digest
		}
	}
	return fallback
}

type MirrorPlan struct {
	Source, Push, Destination Image
	Platform, Kind, Message   string
}

func PlanMirror(action, source, push, destination, platform string) (MirrorPlan, error) {
	p := MirrorPlan{Platform: strings.TrimSpace(platform)}
	var err error
	if p.Source, err = ParseImage(source); err != nil {
		return p, err
	}
	if p.Push, err = ParseImage(push); err != nil {
		return p, err
	}
	if p.Destination, err = ParseImage(destination); err != nil {
		return p, err
	}
	switch action {
	case "mirror-control-image":
		p.Kind = "Control"
	case "mirror-system-image":
		p.Kind = "System"
		if p.Platform == "" {
			p.Platform = "linux/amd64"
		}
	case "cache-runtime-image":
		p.Kind = "Runtime"
	default:
		return p, fmt.Errorf("unsupported image mirror action")
	}
	if p.Platform != "" && p.Platform != "linux/amd64" && p.Platform != "linux/arm64" {
		return p, fmt.Errorf("image mirror platform must be linux/amd64 or linux/arm64")
	}
	// The public reference must name the same content we actually verify through
	// the builder-local registry endpoint; only the registry host may differ.
	if p.Push.Tag == "" || p.Push.Tag != p.Destination.Tag || p.Push.Digest != "" || p.Destination.Digest != "" || p.Push.Path != p.Destination.Path || p.Push.Repository == p.Push.Path || p.Destination.Repository == p.Destination.Path {
		return p, fmt.Errorf("image push and public destinations must have matching repositories and explicit tags")
	}
	valid := p.Kind == "Control" && p.Push.Path == "luma-control"
	valid = valid || p.Kind == "System" && strings.HasPrefix(p.Push.Path, "luma-system/")
	valid = valid || p.Kind == "Runtime" && strings.HasPrefix(p.Push.Path, "luma-cache/")
	if !valid {
		return p, fmt.Errorf("image destination is outside the permitted Luma repository")
	}
	p.Message = p.Kind + " image cached and verified in the internal registry."
	return p, nil
}
