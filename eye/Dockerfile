# Stage 1: Build @nickname4th/luma-eye SDK and embedded Web Console SPA
FROM node:22-alpine AS web-builder
WORKDIR /src

COPY sdk/js/package.json sdk/js/package-lock.json* sdk/js/
RUN cd sdk/js && (npm ci || npm install)
COPY skills/luma-eye/SKILL.md skills/luma-eye/SKILL.md
COPY sdk/js sdk/js
RUN mkdir -p internal/sdkasset internal/skillasset && cd sdk/js && npm run build

COPY web/package.json web/package-lock.json* web/
RUN cd web && (npm ci || npm install)
COPY web web
RUN cd web && npm run build

# Stage 2: Build static Go binary with embedded SDK + Web Console + tzdata
FROM golang:1.26-alpine AS go-builder
WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY cmd cmd
COPY internal internal
COPY --from=web-builder /src/internal/sdkasset/eye.min.js internal/sdkasset/eye.min.js
COPY --from=web-builder /src/internal/sdkasset/eye-replay.min.js internal/sdkasset/eye-replay.min.js
COPY --from=web-builder /src/internal/console/dist internal/console/dist

RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/luma-eye ./cmd/luma-eye

# Stage 3: Minimal production runtime (no apk network dependency)
FROM alpine:3.20
COPY --from=go-builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=go-builder /out/luma-eye /usr/local/bin/luma-eye

WORKDIR /var/lib/luma-eye
ENV LUMA_EYE_DATA_DIR=/var/lib/luma-eye \
    LUMA_EYE_LISTEN=:8090

EXPOSE 8090
ENTRYPOINT ["luma-eye"]
CMD ["serve"]
