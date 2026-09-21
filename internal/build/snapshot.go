package build

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"regexp"
)

const MaxAnalysisArtifactBytes int64 = 16 << 20

var contentDigest = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
var builderReference = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$`)
var artifactLeaseID = regexp.MustCompile(`^artdl_[A-Za-z0-9_-]{32,96}$`)

// Snapshot binds the logical tree to the exact archive consumed by the analyzer.
// Snapshot IDs are task-scoped even when equal content is shared on disk.
type Snapshot struct {
	TreeDigest   string `json:"sourceTreeDigest"`
	Digest       string `json:"sourceSnapshotDigest"`
	SourceBytes  int64  `json:"sourceBytes"`
	ArchiveBytes int64  `json:"archiveBytes"`
}

func ValidContentDigest(value string) bool { return contentDigest.MatchString(value) }

func SnapshotID(taskID, digest string) (string, error) {
	if !builderReference.MatchString(taskID) || !ValidContentDigest(digest) {
		return "", fmt.Errorf("invalid source snapshot binding")
	}
	sum := sha256.Sum256([]byte(taskID + "\x00" + digest))
	return "snapshot-" + hex.EncodeToString(sum[:]), nil
}

type AnalysisArtifact struct {
	Name      string `json:"name"`
	Digest    string `json:"digest"`
	MediaType string `json:"mediaType"`
	SizeBytes int64  `json:"sizeBytes"`
}

// StoreNamespace maps the public closed set of artifact names to existing disk
// locations. Neither API callers nor analyzer output may supply arbitrary paths.
func (a AnalysisArtifact) StoreNamespace() (string, error) {
	var name, media string
	switch a.Name {
	case "evidence":
		name, media = "evidence", "application/vnd.lae.evidence+json"
	case "deploymentPlan":
		name, media = "deployment-plan", "application/vnd.lae.deployment-plan+json"
	case "buildPlan":
		name, media = "build-plan-candidate", "application/vnd.lae.build-plan-candidate+json"
	}
	if name == "" || media != a.MediaType || !ValidContentDigest(a.Digest) || a.SizeBytes < 1 || a.SizeBytes > MaxAnalysisArtifactBytes {
		return "", fmt.Errorf("invalid analysis artifact descriptor")
	}
	return "artifacts/" + name + "/sha256", nil
}

type ArtifactExportRequest struct {
	LeaseID       string           `json:"leaseId"`
	BuilderTaskID string           `json:"builderTaskId"`
	Artifact      AnalysisArtifact `json:"artifact"`
}

func (r ArtifactExportRequest) Validate() error {
	if !artifactLeaseID.MatchString(r.LeaseID) || !builderReference.MatchString(r.BuilderTaskID) {
		return fmt.Errorf("invalid artifact export binding")
	}
	_, err := r.Artifact.StoreNamespace()
	return err
}
