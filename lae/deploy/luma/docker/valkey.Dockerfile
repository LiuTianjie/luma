# syntax=docker/dockerfile:1.7

# Repository imports build every runtime image on the configured Luma Builder
# and publish the immutable result to the internal registry. Keeping Valkey as
# a build input prevents runtime nodes from depending on Docker Hub egress.
FROM valkey/valkey:9.1.0-alpine@sha256:a35428eba9043cc0b79dbe54100f0c92784f2de00ad09b01182bfb1c5c83d1bd

USER 999:999
