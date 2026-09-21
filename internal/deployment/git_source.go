package deployment

// GitSource keeps rebuild inputs and provenance with a deployment. Credential
// values and deployment secrets are deliberately absent from this type.
type GitSource struct {
	Region         string `json:"region,omitempty"`
	Exposure       string `json:"exposure,omitempty"`
	Domain         string `json:"domain,omitempty"`
	Port           int    `json:"port,omitempty"`
	RepoURL        string `json:"repoUrl"`
	ProviderID     string `json:"providerId,omitempty"`
	Repository     string `json:"repository,omitempty"`
	Ref            string `json:"ref,omitempty"`
	BuildNode      string `json:"buildNode"`
	Context        string `json:"context,omitempty"`
	Dockerfile     string `json:"dockerfile,omitempty"`
	Platform       string `json:"platform,omitempty"`
	RegistryHost   string `json:"registryHost"`
	PushHost       string `json:"pushHost"`
	Manifest       string `json:"manifest,omitempty"`
	ComposeContent string `json:"composeContent,omitempty"`
	ComposeSidecar string `json:"composeSidecar,omitempty"`
	ProxyMode      string `json:"proxyMode,omitempty"`
	BuildRunID     string `json:"buildRunId"`
	Revision       string `json:"revision"`
}
