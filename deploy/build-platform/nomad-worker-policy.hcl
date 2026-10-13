# Bind only to the build-platform service. Executor tokens have no Nomad API ACL.
namespace "builds" {
  capabilities = ["read-job", "submit-job", "dispatch-job", "list-jobs"]
  variables {
    path "nomad/jobs/luma-bld2-*" {
      capabilities = ["read", "write", "destroy"]
    }
  }
}

node {
  policy = "read"
}
