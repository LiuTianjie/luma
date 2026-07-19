# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM node:22.21.0-bookworm-slim@sha256:f9f7f95dcf1f007b007c4dcd44ea8f7773f931b71dc79d57c216e731c87a090b AS build

ARG NEXT_PUBLIC_LAE_UPLOAD_ORIGINS

ENV LAE_API_INTERNAL_URL=http://api:8080 \
    NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PUBLIC_LAE_UPLOAD_ORIGINS="${NEXT_PUBLIC_LAE_UPLOAD_ORIGINS}" \
    PNPM_HOME=/pnpm
ENV PATH="${PNPM_HOME}:${PATH}"

WORKDIR /workspace

# Dependency downloads use the Builder's declared egress policy. Proxy values
# are BuildKit transport inputs and are not persisted in the runtime image.
RUN corepack enable && corepack prepare pnpm@10.30.0 --activate

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/package.json
RUN pnpm install --frozen-lockfile --filter @lae/web...

COPY apps/web apps/web
RUN pnpm --filter @lae/web build && \
    node -e "const m=require('./apps/web/.next/routes-manifest.json');const r=m.rewrites.afterFiles.find(x=>x.source==='/v1/:path*');if(!r||r.destination!=='http://api:8080/v1/:path*')throw new Error('production API rewrite is not container-internal')"

FROM node:22.21.0-bookworm-slim@sha256:f9f7f95dcf1f007b007c4dcd44ea8f7773f931b71dc79d57c216e731c87a090b AS runtime

ENV HOSTNAME=0.0.0.0 \
    NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production \
    PORT=3000

WORKDIR /app

COPY --from=build --chown=node:node /workspace/apps/web/.next/standalone ./
COPY --from=build --chown=node:node /workspace/apps/web/.next/static ./apps/web/.next/static

USER node

EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:3000/').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"]

CMD ["node", "apps/web/server.js"]
