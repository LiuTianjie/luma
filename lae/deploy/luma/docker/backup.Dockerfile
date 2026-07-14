# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727 AS minio-client

# The Builder-owned registry is the release source of truth for system images.
# This image contains the matching PostgreSQL 17 client tools used by staging.
FROM 100.66.177.70:5000/luma-system/postgres-amd64:17@sha256:5aee909f99ab78c62f03636b6ca25a17195657605ce6782d9919ce4288595eda

COPY --from=minio-client /usr/bin/mc /usr/local/bin/mc
COPY --chmod=0555 deploy/luma/docker/backup.sh /usr/local/bin/lae-backup

ENV HOME=/tmp \
    MC_CONFIG_DIR=/tmp/.mc

USER 999:999

HEALTHCHECK --interval=5m --timeout=10s --start-period=20m --retries=3 \
  CMD ["/usr/local/bin/lae-backup", "health"]

ENTRYPOINT ["/usr/local/bin/lae-backup"]
CMD ["run"]
