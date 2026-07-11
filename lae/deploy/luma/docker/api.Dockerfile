# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b AS uv

FROM python:3.12.13-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/lae/.venv \
    UV_PYTHON_DOWNLOADS=never

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY . .
RUN uv sync --frozen --no-dev --no-editable --package lae-api && \
    uv sync --frozen --no-dev --no-editable --package lae-store --extra migrations --inexact

FROM python:3.12.13-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b AS runtime

ENV PATH=/opt/lae/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 lae && \
    useradd --uid 10001 --gid 10001 --create-home --home-dir /home/lae lae

WORKDIR /opt/lae
COPY --from=build /opt/lae/.venv /opt/lae/.venv
COPY --from=build /src/migrations /opt/lae/migrations
COPY --chmod=0555 deploy/luma/docker/api-entrypoint.sh /usr/local/bin/lae-api-entrypoint

USER 10001:10001

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=3).read()"]

ENTRYPOINT ["/usr/local/bin/lae-api-entrypoint"]
