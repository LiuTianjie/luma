# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727

ENV HOME=/tmp \
    MC_CONFIG_DIR=/tmp/.mc

COPY --chmod=0555 deploy/luma/docker/artifact-init.sh /usr/local/bin/lae-artifact-init

USER 10001:10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=5 \
  CMD ["/bin/sh", "-ec", "test -f /tmp/lae-artifact-init.ready"]

ENTRYPOINT ["/usr/local/bin/lae-artifact-init"]
