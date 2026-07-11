# LAE Luma Adapter

This package is the dependency-free boundary between the LAE worker and Luma's
`luma.builder-task/v1` API. It deliberately does not import the Luma source
tree.

The public protocol covers only Builder Task v1 for now:

- create `analyze-source` and `build-plan` tasks;
- get task state;
- replay task events from a cursor;
- request cancellation.

`HttpLumaBuilderAdapter` uses Python's standard-library HTTP client.
`FakeLuma` provides a principal- and tenant-scoped in-memory implementation for
worker tests. Response decoding drops Luma node identity, credential lease
references, internal image references, internal addresses, and arbitrary
upstream messages before a model can reach tenant-facing code.

A `succeeded` task is accepted only when its result matches Control's complete
closed contract. Analyze artifacts use their three fixed LAE media types and
must bind to the corresponding top-level digests. Build image references must
bind to `imageDigests`; every image must have exactly one SBOM, provenance and
vulnerability-scan descriptor whose key, media type and digest bind to the
matching result map. External images use the explicit
`application/vnd.lae.external-resolution+json` provenance media type; this is
LAE resolution evidence, not upstream SLSA provenance. The raw internal image references are validated and then
discarded from the tenant-safe model.

The package is a root `uv` workspace member and is consumed by
`services/worker` through a workspace dependency.
