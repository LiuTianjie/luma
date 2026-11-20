"""Control settings routes: secrets, regions, DNS zones, registries, git providers, and CDN."""

from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from ..schemas import (
    CdnCheckRequest,
    CdnPurgeRequest,
    CdnRemoveRequest,
    DnsZoneAddRequest,
    DnsZoneCheckRequest,
    DnsZoneRemoveRequest,
    GitProviderRemoveRequest,
    GitProviderSetRequest,
    RegionCreateRequest,
    RegionRemoveRequest,
    RegionUpdateRequest,
    RegistryRemoveRequest,
    RegistrySetRequest,
    SecretRemoveRequest,
    SecretSetRequest,
)
from .deps import AuthenticatedAPIRoute, require_authenticated_token

router = APIRouter(route_class=AuthenticatedAPIRoute, tags=["settings"])


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


@router.get("/v1/secrets", summary="List configured secret names")
async def list_secrets(token: str = Depends(require_authenticated_token)) -> Response:
    from .. import server

    return server._json_response(200, await run_in_threadpool(server.handle_secret_list, token))


@router.post("/v1/secrets", summary="Create or update a Control or scoped secret")
async def set_secret(
    payload: SecretSetRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_secret_set, token, payload.to_payload())
    )


@router.post("/v1/secrets/remove", summary="Remove a Control or scoped secret")
async def remove_secret(
    payload: SecretRemoveRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_secret_remove, token, payload.to_payload())
    )


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------


@router.get("/v1/regions", summary="List built-in and custom regions")
async def list_regions(token: str = Depends(require_authenticated_token)) -> Response:
    from .. import server

    return server._json_response(200, await run_in_threadpool(server.handle_region_list, token))


@router.post("/v1/regions", summary="Create a custom region")
async def create_region(
    payload: RegionCreateRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_region_create, token, payload.to_payload())
    )


@router.post("/v1/regions/update", summary="Update a custom region's egress or exposures")
async def update_region(
    payload: RegionUpdateRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_region_update, token, payload.to_payload())
    )


@router.post("/v1/regions/remove", summary="Remove an unused custom region")
async def remove_region(
    payload: RegionRemoveRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_region_remove, token, payload.to_payload())
    )


# ---------------------------------------------------------------------------
# DNS Zones
# ---------------------------------------------------------------------------


@router.get("/v1/dns/zones", summary="List primary and catalog DNS zones")
async def list_dns_zones(token: str = Depends(require_authenticated_token)) -> Response:
    from .. import server

    return server._json_response(200, await run_in_threadpool(server.handle_dns_zone_list, token))


@router.post("/v1/dns/zones", summary="Add or update a catalog DNS zone")
async def add_dns_zone(
    payload: DnsZoneAddRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_dns_zone_add, token, payload.to_payload())
    )


@router.post("/v1/dns/zones/check", summary="Verify credentials for a DNS zone")
async def check_dns_zone(
    payload: DnsZoneCheckRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_dns_zone_check, token, payload.to_payload())
    )


@router.post("/v1/dns/zones/remove", summary="Remove an unused catalog DNS zone")
async def remove_dns_zone(
    payload: DnsZoneRemoveRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_dns_zone_remove, token, payload.to_payload())
    )


# ---------------------------------------------------------------------------
# Container Registries
# ---------------------------------------------------------------------------


@router.get("/v1/registries", summary="List configured container registries")
async def list_registries(token: str = Depends(require_authenticated_token)) -> Response:
    from .. import server

    return server._json_response(200, await run_in_threadpool(server.handle_registry_list, token))


@router.post("/v1/registries", summary="Create or update container registry credentials")
async def set_registry(
    payload: RegistrySetRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_registry_set, token, payload.to_payload())
    )


@router.post("/v1/registries/remove", summary="Remove container registry credentials")
async def remove_registry(
    payload: RegistryRemoveRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_registry_remove, token, payload.to_payload())
    )


# ---------------------------------------------------------------------------
# Git Providers
# ---------------------------------------------------------------------------


@router.get("/v1/git-providers", summary="List configured Git providers")
async def list_git_providers(token: str = Depends(require_authenticated_token)) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_git_provider_list, token)
    )


@router.post("/v1/git-providers", summary="Create or update a Git provider account")
async def set_git_provider(
    payload: GitProviderSetRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_git_provider_set, token, payload.to_payload())
    )


@router.post("/v1/git-providers/remove", summary="Remove a Git provider account")
async def remove_git_provider(
    payload: GitProviderRemoveRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200,
        await run_in_threadpool(server.handle_git_provider_remove, token, payload.to_payload()),
    )


@router.get(
    "/v1/git-providers/{provider_id}/repositories",
    summary="List repositories accessible via a Git provider",
)
async def list_git_provider_repositories(
    provider_id: str,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200,
        await run_in_threadpool(
            server.handle_git_provider_repositories,
            token,
            urllib.parse.unquote(provider_id),
        ),
    )


@router.get(
    "/v1/git-providers/{provider_id}/repositories/{owner}/{repo}/refs",
    summary="List branches and tags for a Git provider repository",
)
async def list_git_provider_refs(
    provider_id: str,
    owner: str,
    repo: str,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    repository = f"{urllib.parse.unquote(owner)}/{urllib.parse.unquote(repo)}"
    return server._json_response(
        200,
        await run_in_threadpool(
            server.handle_git_provider_refs,
            token,
            urllib.parse.unquote(provider_id),
            repository,
        ),
    )


# ---------------------------------------------------------------------------
# CDN
# ---------------------------------------------------------------------------


@router.get("/v1/cdn", summary="List CDN-managed domains and statuses")
async def list_cdn(token: str = Depends(require_authenticated_token)) -> Response:
    from .. import server

    return server._json_response(200, await run_in_threadpool(server.handle_cdn_list, token))


@router.post("/v1/cdn/check", summary="Verify CDN provider setup and credentials")
async def check_cdn(
    payload: CdnCheckRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_cdn_check, token, payload.to_payload())
    )


@router.post("/v1/cdn/purge", summary="Purge CDN cache for a domain")
async def purge_cdn(
    payload: CdnPurgeRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_cdn_purge, token, payload.to_payload())
    )


@router.post("/v1/cdn/remove", summary="Remove an unused CDN-managed domain")
async def remove_cdn(
    payload: CdnRemoveRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_cdn_remove, token, payload.to_payload())
    )
