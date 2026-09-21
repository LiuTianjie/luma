package build

// LocalSource describes the working tree, not a fresh checkout of its remote.
// Target nodes/regions let Control reserve the correct image architectures.
type LocalSource struct {
	Path          string   `json:"path"`
	RepoURL       string   `json:"repoUrl"`
	Revision      string   `json:"sourceRevision"`
	Name          string   `json:"name"`
	Kind          string   `json:"kind"`
	TargetNodes   []string `json:"targetNodes"`
	TargetRegions []string `json:"targetRegions"`
}
type LocalImageRequest struct {
	ImageRequest
	Path          string
	Tag           string
	Builder       string
	DockerConfig  string
	DockerContext string
}

type LocalPrepareRequest struct {
	ControlRequest
	TargetNodes   []string `json:"targetNodes,omitempty"`
	TargetRegions []string `json:"targetRegions,omitempty"`
	TargetNode    string   `json:"targetNode,omitempty"`
	TargetRegion  string   `json:"targetRegion,omitempty"`
}
type LocalCompleteRequest struct {
	Queue       bool              `json:"queue"`
	BuildResult ImageResult       `json:"buildResult"`
	EnvSecrets  map[string]string `json:"envSecrets"`
}
type LocalUpload struct {
	RegistryHost string `json:"registryHost"`
	Repository   string `json:"repository"`
	Tag          string `json:"tag"`
	Image        string `json:"image"`
	Platform     string `json:"platform"`
}
type LocalReservation struct {
	Run    Run         `json:"run"`
	Upload LocalUpload `json:"upload"`
}
