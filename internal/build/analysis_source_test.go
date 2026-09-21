package build

import (
	"strings"
	"testing"
)

func sourceRequest() AnalysisRequest {
	return AnalysisRequest{BuilderTaskID: "task", ExternalOperationID: "operation", TenantRef: "tenant", ApplicationRef: "application", CredentialLeaseID: "lease", PolicyVersion: "policy-v1", AgentImageDigest: "registry.test/analyzer@sha256:" + strings.Repeat("a", 64), SourceRef: SourceReference{Repository: "https://git.test/repo.git"}, Limits: AnalysisLimits{CPU: 1, MemoryMiB: 256, DiskMiB: 256, TimeoutSeconds: 30}}
}

func TestAnalysisSourceScopeAndUnionValidation(t *testing.T) {
	valid := sourceRequest()
	if _, err := valid.ValidateSource(); err != nil {
		t.Fatal(err)
	}
	object := valid
	object.SourceRef = SourceReference{Kind: "object", Digest: "sha256:" + strings.Repeat("b", 64), MediaType: "application/zip", SizeBytes: 1024}
	object.ObjectURL = "https://uploads.test:8443/object?signature=private"
	object.ObjectAllowedHost = "uploads.test"
	if _, err := object.ValidateSource(); err != nil {
		t.Fatal(err)
	}
	cases := map[string]AnalysisRequest{}
	change := func(name string, original AnalysisRequest, f func(*AnalysisRequest)) {
		f(&original)
		cases[name] = original
	}
	change("HTTP git", valid, func(r *AnalysisRequest) { r.SourceRef.Repository = "http://git.test/repo.git" })
	change("inline credential", valid, func(r *AnalysisRequest) { r.SourceRef.Repository = "https://user:secret@git.test/repo.git" })
	change("Git option", valid, func(r *AnalysisRequest) { r.SourceRef.Ref = "--upload-pack=command" })
	change("subdirectory traversal", valid, func(r *AnalysisRequest) { r.SourceRef.Subdirectory = "../other" })
	change("Git object fields", valid, func(r *AnalysisRequest) { r.SourceRef.Digest = "sha256:" + strings.Repeat("a", 64) })
	change("wrong kind", valid, func(r *AnalysisRequest) { r.SourceRef.Kind = "other" })
	change("unbound task", valid, func(r *AnalysisRequest) { r.BuilderTaskID = "" })
	change("unbounded memory", valid, func(r *AnalysisRequest) { r.Limits.MemoryMiB = 1 })
	change("object credential", object, func(r *AnalysisRequest) { r.GitToken = "private" })
	change("object repository", object, func(r *AnalysisRequest) { r.SourceRef.Repository = valid.SourceRef.Repository })
	change("host mismatch", object, func(r *AnalysisRequest) { r.ObjectURL = "https://other.test/object" })
	change("host suffix", object, func(r *AnalysisRequest) { r.ObjectURL = "https://uploads.test.other/object" })
	change("bad port", object, func(r *AnalysisRequest) { r.ObjectURL = "https://uploads.test:65536/object" })
	change("object userinfo", object, func(r *AnalysisRequest) { r.ObjectURL = "https://secret@uploads.test/object" })
	change("object HTTP", object, func(r *AnalysisRequest) { r.ObjectURL = "http://uploads.test/object" })
	change("unbound URL", object, func(r *AnalysisRequest) { r.ObjectAllowedHost = "" })
	change("oversized object", object, func(r *AnalysisRequest) { r.SourceRef.SizeBytes = 513 << 20 })
	for name, r := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := r.ValidateSource(); err == nil {
				t.Fatal("invalid source accepted")
			}
		})
	}
}
