# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM python:3.12.13-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 lae && \
    useradd --uid 10001 --gid 10001 --create-home --home-dir /home/lae lae

WORKDIR /opt/lae/scripts
COPY --chmod=0444 scripts/staging_product_e2e.py scripts/template_smoke.py ./
COPY --chmod=0555 deploy/luma/docker/template-smoke-entrypoint.sh /usr/local/bin/lae-template-smoke-entrypoint

USER 10001:10001

HEALTHCHECK --interval=5m --timeout=5s --start-period=10m --retries=3 \
  CMD ["python", "-c", "import os,time; p='/tmp/lae-template-smoke-heartbeat'; raise SystemExit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<172800 else 1)"]

ENTRYPOINT ["/usr/local/bin/lae-template-smoke-entrypoint"]
