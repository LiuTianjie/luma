package build

import (
	"context"
	"fmt"
	"io"
	"math"
	"net/url"
	"path"
	"regexp"
	"strconv"
	"strings"
)

type SourceLimits struct {
	MaxBytes   int64
	MaxEntries int
}

var analysisObjectHost = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$`)

func (l SourceLimits) Validate() error {
	if l.MaxBytes < 1 || l.MaxBytes > 1<<50 || l.MaxEntries < 1 || l.MaxEntries > 2_000_000 {
		return fmt.Errorf("invalid snapshot limits")
	}
	return nil
}

type AnalysisLimits struct {
	CPU            float64 `json:"cpu"`
	MemoryMiB      int     `json:"memoryMiB"`
	DiskMiB        int64   `json:"diskMiB"`
	TimeoutSeconds int     `json:"timeoutSeconds"`
}

func (l AnalysisLimits) Validate() error {
	if math.IsNaN(l.CPU) || math.IsInf(l.CPU, 0) || l.CPU < .1 || l.CPU > 32 || l.MemoryMiB < 128 || l.MemoryMiB > 131072 || l.DiskMiB < 256 || l.DiskMiB > 1048576 || l.TimeoutSeconds < 10 || l.TimeoutSeconds > 14400 {
		return fmt.Errorf("invalid source analysis limits")
	}
	return nil
}

// SourceReference is a closed union. Git sources cannot include object download
// metadata, and uploaded objects cannot carry Git credentials or repository paths.
type SourceReference struct {
	Kind         string `json:"kind,omitempty"`
	Repository   string `json:"repository,omitempty"`
	Ref          string `json:"ref,omitempty"`
	Subdirectory string `json:"subdirectory,omitempty"`
	Digest       string `json:"digest,omitempty"`
	MediaType    string `json:"mediaType,omitempty"`
	SizeBytes    int64  `json:"sizeBytes,omitempty"`
}

type AnalysisRequest struct {
	BuilderTaskID       string          `json:"builderTaskId"`
	ExternalOperationID string          `json:"externalOperationId"`
	TenantRef           string          `json:"tenantRef"`
	ApplicationRef      string          `json:"applicationRef"`
	CredentialLeaseID   string          `json:"credentialLeaseId"`
	PolicyVersion       string          `json:"policyVersion"`
	AgentImageDigest    string          `json:"agentImageDigest"`
	SourceRef           SourceReference `json:"sourceRef"`
	Limits              AnalysisLimits  `json:"limits"`
	ExternalRegistries  []string        `json:"externalRegistries"`
	// These values are ephemeral lease enrichment, never part of public metadata.
	GitToken          string `json:"gitToken,omitempty"`
	GitUsername       string `json:"gitUsername,omitempty"`
	ObjectURL         string `json:"objectUrl,omitempty"`
	ObjectAllowedHost string `json:"objectAllowedHost,omitempty"`
}

func (r AnalysisRequest) ValidateSource() (AnalysisRequest, error) {
	for _, value := range []string{r.BuilderTaskID, r.ExternalOperationID, r.TenantRef, r.ApplicationRef, r.CredentialLeaseID, r.PolicyVersion} {
		if !builderReference.MatchString(value) {
			return r, fmt.Errorf("invalid source analysis binding")
		}
	}
	if err := r.Limits.Validate(); err != nil {
		return r, err
	}
	image, digest, ok := strings.Cut(r.AgentImageDigest, "@")
	if !ok || len(r.AgentImageDigest) > 1024 || image == "" || strings.ContainsAny(image, " \r\n\t@") || !ValidContentDigest(digest) {
		return r, fmt.Errorf("analysis runner must be an immutable image reference")
	}
	source := &r.SourceRef
	if source.Kind == "object" {
		if source.Repository != "" || source.Ref != "" || source.Subdirectory != "" || r.GitToken != "" || r.GitUsername != "" || !ValidContentDigest(source.Digest) || (source.MediaType != "text/html" && source.MediaType != "application/zip") || source.SizeBytes < 1 || source.SizeBytes > 512<<20 {
			return r, fmt.Errorf("invalid uploaded source reference")
		}
		if source.SizeBytes > r.Limits.DiskMiB<<20 {
			return r, fmt.Errorf("uploaded source exceeds analysis disk budget")
		}
		u, err := url.Parse(r.ObjectURL)
		if err != nil || u.Scheme != "https" || u.User != nil || u.Fragment != "" || u.Opaque != "" || u.Host == "" || len(r.ObjectURL) > 8192 || strings.ContainsAny(r.ObjectURL, "\x00\r\n\t ") {
			return r, fmt.Errorf("invalid uploaded source URL")
		}
		if len(r.ObjectAllowedHost) > 253 || !analysisObjectHost.MatchString(r.ObjectAllowedHost) || u.Hostname() != r.ObjectAllowedHost {
			return r, fmt.Errorf("uploaded source URL does not match the leased host")
		}
		if u.Port() != "" {
			port, err := strconv.Atoi(u.Port())
			if err != nil || port < 1 || port > 65535 {
				return r, fmt.Errorf("invalid uploaded source port")
			}
		}
		return r, nil
	}
	if source.Kind != "" || source.Digest != "" || source.MediaType != "" || source.SizeBytes != 0 || r.ObjectURL != "" || r.ObjectAllowedHost != "" {
		return r, fmt.Errorf("invalid Git source reference")
	}
	if len(source.Repository) > 2048 {
		return r, fmt.Errorf("invalid analysis repository")
	}
	repo, err := NormalizeRepository(source.Repository)
	if err != nil || !strings.HasPrefix(repo, "https://") {
		return r, fmt.Errorf("analysis source requires an HTTPS repository without credentials")
	}
	source.Repository = repo
	if len(source.Ref) > 512 || strings.HasPrefix(source.Ref, "-") || strings.ContainsAny(source.Ref, "\x00\r\n\t ") {
		return r, fmt.Errorf("invalid analysis Git ref")
	}
	if source.Subdirectory == "." {
		source.Subdirectory = ""
	}
	if source.Subdirectory != "" && (len(source.Subdirectory) > 2048 || path.Clean(source.Subdirectory) != source.Subdirectory || strings.HasPrefix(source.Subdirectory, "/") || strings.ContainsAny(source.Subdirectory, "\x00\\") || source.Subdirectory == ".." || strings.HasPrefix(source.Subdirectory, "../")) {
		return r, fmt.Errorf("invalid analysis source subdirectory")
	}
	if len(r.GitToken) > 8192 || len(r.GitUsername) > 256 || strings.ContainsAny(r.GitToken+r.GitUsername, "\x00\r\n") {
		return r, fmt.Errorf("invalid leased Git credential")
	}
	return r, nil
}

type AnalysisMetadata struct {
	SchemaVersion        string `json:"schemaVersion"`
	BuilderTaskID        string `json:"builderTaskId"`
	ExternalOperationID  string `json:"externalOperationId"`
	TenantRef            string `json:"tenantRef"`
	ApplicationRef       string `json:"applicationRef"`
	ResolvedCommit       string `json:"resolvedCommit"`
	SourceTreeDigest     string `json:"sourceTreeDigest"`
	SourceSnapshotID     string `json:"sourceSnapshotId"`
	SourceSnapshotDigest string `json:"sourceSnapshotDigest"`
	PolicyVersion        string `json:"policyVersion"`
	AgentImageDigest     string `json:"agentImageDigest"`
}

type AnalysisSourceFiles interface {
	Capture(context.Context, string, string, SourceLimits) (Snapshot, error)
	PutSnapshot(context.Context, io.ReadSeeker, string, SourceLimits) error
	Materialize(context.Context, io.ReadSeeker, string, string, SourceLimits) error
	MaterializeObject(context.Context, string, string, string, SourceLimits) error
}
