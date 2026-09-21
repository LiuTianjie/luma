package build

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/LiuTianjie/luma/internal/deployment"
)

var ErrInput = errors.New("invalid build request")
var ErrState = errors.New("build state changed")
var ErrNotFound = errors.New("build run not found")

type QueuedWorkflow struct {
	deployment.Workflow
	AcceptChange bool   `json:"acceptChange,omitempty"`
	CheckedName  string `json:"checkedName,omitempty"`
}
type RetryRequest struct {
	Queue      bool              `json:"queue"`
	EnvSecrets map[string]string `json:"envSecrets"`
	Workflow   *QueuedWorkflow   `json:"workflow,omitempty"`
}
type UpdateRequest struct {
	RetryRequest
	Name  string `json:"name"`
	Stack string `json:"stack,omitempty"`
}
type ControlRequest struct {
	SourceRevision string            `json:"sourceRevision,omitempty"`
	Workflow       *QueuedWorkflow   `json:"workflow,omitempty"`
	ExpectedName   string            `json:"expectedName,omitempty"`
	RepoURL        string            `json:"repoUrl,omitempty"`
	ProviderID     string            `json:"providerId,omitempty"`
	Repository     string            `json:"repository,omitempty"`
	BuildNode      string            `json:"buildNode,omitempty"`
	Ref            string            `json:"ref,omitempty"`
	RegistryHost   string            `json:"registryHost,omitempty"`
	PushHost       string            `json:"pushHost,omitempty"`
	Context        string            `json:"context,omitempty"`
	Dockerfile     string            `json:"dockerfile,omitempty"`
	Platform       string            `json:"platform,omitempty"`
	Region         string            `json:"region,omitempty"`
	Exposure       string            `json:"exposure,omitempty"`
	Domain         string            `json:"domain,omitempty"`
	Port           int               `json:"port,omitempty"`
	ProxyMode      string            `json:"proxyMode,omitempty"`
	Proxy          *string           `json:"proxy,omitempty"`
	BuildTimeout   int               `json:"buildTimeout,omitempty"`
	ComposeSidecar string            `json:"composeSidecar,omitempty"`
	Manifest       string            `json:"manifest,omitempty"`
	ComposeContent string            `json:"composeContent,omitempty"`
	Queue          bool              `json:"queue,omitempty"`
	SkipDNS        bool              `json:"skipDns,omitempty"`
	Origin         string            `json:"origin,omitempty"`
	EnvSecrets     map[string]string `json:"envSecrets"`
}

func (r ControlRequest) Public() ControlRequest {
	r.EnvSecrets = nil
	r.Workflow = nil
	if r.Proxy != nil && *r.Proxy != "" {
		r.Proxy = nil
	}
	return r
}

type Run struct {
	ExpiresAt          int64          `json:"expiresAt,omitempty"`
	RetryOf            string         `json:"retryOf,omitempty"`
	RetryRootID        string         `json:"retryRootId,omitempty"`
	ID                 string         `json:"id"`
	Status             string         `json:"status"`
	Source             string         `json:"source"`
	BuildNode          string         `json:"buildNode"`
	ProjectKey         string         `json:"projectKey"`
	Mode               string         `json:"mode"`
	Request            ControlRequest `json:"request"`
	AgentTaskID        string         `json:"agentTaskId,omitempty"`
	Message            string         `json:"message"`
	Result             map[string]any `json:"result"`
	CreatedAt          int64          `json:"createdAt"`
	UpdatedAt          int64          `json:"updatedAt"`
	CompletedAt        int64          `json:"completedAt,omitempty"`
	CancelRequestedAt  int64          `json:"cancelRequestedAt,omitempty"`
	CanceledAt         int64          `json:"canceledAt,omitempty"`
	QueueOrder         int64          `json:"queueOrder"`
	QueueManaged       bool           `json:"queueManaged"`
	QueueExecuting     bool           `json:"queueExecuting"`
	QueueOwnerReleased bool           `json:"queueOwnerReleased"`
	SupersededBy       string         `json:"supersededBy,omitempty"`
	ReplacementKey     string         `json:"replacementKey,omitempty"`
	GoBuild            bool           `json:"goBuild"`
}

func Terminal(status string) bool {
	return status == "succeeded" || status == "failed" || status == "canceled"
}
func (r Run) HoldsProject() bool {
	return r.QueueExecuting || (!(r.Mode == "local" && r.Request.Queue && !r.QueueManaged) && (r.Status == "running" || r.Status == "canceling" || r.Status == "finalizing"))
}

// One project runs serially; different targets queue FIFO. A known replacement
// target also waits for its previous worker/remote child to stop across projects.
func CanClaim(candidate Run, others []Run) bool {
	if candidate.Status != "queued" || !candidate.GoBuild {
		return false
	}
	for _, r := range others {
		if r.ID == candidate.ID {
			continue
		}
		sameProject := r.ProjectKey == candidate.ProjectKey
		sameTarget := candidate.ReplacementKey != "" && r.ReplacementKey == candidate.ReplacementKey
		if (sameProject || sameTarget) && r.HoldsProject() {
			return false
		}
		if sameProject && r.Status == "queued" && (r.QueueOrder < candidate.QueueOrder || r.QueueOrder == candidate.QueueOrder && r.ID < candidate.ID) {
			return false
		}
	}
	return true
}

type Work struct {
	LocalResult *ImageResult
	Run         Run
	Request     ControlRequest
}
type Event struct {
	Name      string `json:"name"`
	Status    string `json:"status"`
	Message   string `json:"message"`
	Timestamp int64  `json:"ts"`
}
type Submission struct {
	Run     Run
	Request ControlRequest
}
type QueueRepository interface {
	EnqueueBuild(context.Context, string, Submission) (Run, error)
	ClaimBuild(context.Context) (*Work, error)
	ReadBuild(context.Context, string) (Run, error)
	CancelBuild(context.Context, string, string) (Run, error)
	BuildEvent(context.Context, string, Event) error
	FinishBuild(context.Context, string, string, map[string]any, string) error
	RecoverBuilds(context.Context) error
	BeginBuildDeployment(context.Context, string) error
	AttachBuildTask(context.Context, string, json.RawMessage) (string, error)
}
