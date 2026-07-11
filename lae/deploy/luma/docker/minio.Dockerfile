# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

# MinIO stopped publishing an official container for its 2025-10-15 security
# release. Build the verified upstream release commit so LAE does not run the
# older service-account/session-policy implementation that preceded that fix.
FROM golang:1.24.8-bookworm@sha256:4ed690d6649d63c312b99a6120025ec79ce3b542968a37da53d6236c7c61a848 AS build

ARG MINIO_RELEASE=RELEASE.2025-10-15T17-29-55Z
ARG MINIO_COMMIT=9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a

WORKDIR /src

RUN git clone --depth 1 --branch "${MINIO_RELEASE}" https://github.com/minio/minio.git . && \
    test "$(git rev-parse HEAD)" = "${MINIO_COMMIT}" && \
    LDFLAGS="$(go run buildscripts/gen-ldflags.go)" && \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
      go build -tags kqueue -trimpath --ldflags "${LDFLAGS}" -o /out/minio .

COPY deploy/luma/docker/minio-healthcheck.go /tmp/minio-healthcheck.go
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
      go build -trimpath -ldflags='-s -w' -o /out/lae-minio-healthcheck /tmp/minio-healthcheck.go && \
    mkdir -p /rootfs/data /rootfs/tmp && \
    chmod 1777 /rootfs/tmp && \
    chmod 0700 /rootfs/data

FROM scratch

LABEL org.opencontainers.image.source="https://github.com/minio/minio" \
      org.opencontainers.image.revision="9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a" \
      org.opencontainers.image.version="RELEASE.2025-10-15T17-29-55Z"

ENV HOME=/tmp \
    MINIO_BROWSER=off

COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build --chown=10001:10001 /rootfs/ /
COPY --from=build --chown=10001:10001 /out/minio /usr/local/bin/minio
COPY --from=build --chown=10001:10001 /out/lae-minio-healthcheck /usr/local/bin/lae-minio-healthcheck

USER 10001:10001

EXPOSE 9000 9001
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
  CMD ["/usr/local/bin/lae-minio-healthcheck"]

ENTRYPOINT ["/usr/local/bin/minio"]
CMD ["server", "/data", "--address", ":9000", "--console-address", ":9001"]
