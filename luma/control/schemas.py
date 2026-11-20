"""Pydantic v2 request schemas for Luma Control's FastAPI routers.

All request models set ``extra="allow"`` so older Luma CLIs passing legacy or
additional fields stay 100% wire-compatible while still documenting canonical
fields in the authenticated OpenAPI 3.1 schema.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class LumaRequestModel(BaseModel):
    """Base request schema preserving unknown/legacy CLI fields."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def to_payload(self) -> Dict[str, Any]:
        """Serialize explicitly supplied fields (plus extras) for handlers."""
        return self.model_dump(by_alias=True, exclude_unset=True)


# ---------------------------------------------------------------------------
# System / Workflows
# ---------------------------------------------------------------------------


class WorkflowRecordRequest(LumaRequestModel):
    name: Optional[str] = Field(default=None, description="Application workflow name")
    recipe: Optional[Dict[str, Any]] = Field(default=None, description="Declarative deployment recipe")
    source: Optional[str] = Field(default=None, description="Record source ('manual' or 'cli-success')")
    note: Optional[str] = Field(default=None, description="Optional operator note")
    selector: Optional[Dict[str, Any]] = Field(default=None, description="Optional workflow selector")
    evidence: Optional[Dict[str, Any]] = Field(default=None, description="Optional deployment evidence")
    expectedRevision: Optional[int] = Field(default=None, description="Optimistic concurrency revision")


class WorkflowCheckRequest(LumaRequestModel):
    recipe: Optional[Dict[str, Any]] = Field(default=None, description="Requested deployment recipe")
    selector: Optional[Dict[str, Any]] = Field(default=None, description="Optional workflow selector")


# ---------------------------------------------------------------------------
# Addons
# ---------------------------------------------------------------------------


class AddonConfigureRequest(LumaRequestModel):
    preset: Optional[str] = Field(default=None, description="Deployment preset identifier")
    config: Optional[Dict[str, Any]] = Field(default=None, description="Addon configuration fields")
    secrets: Optional[Dict[str, Any]] = Field(default=None, description="Scoped secrets to store for this addon")
    inherit: Optional[Dict[str, Any]] = Field(default=None, description="Saved Control credentials to inherit")
    rotateSecrets: Optional[List[str]] = Field(default=None, description="Auto-generated secret names to rotate")
    requireComplete: Optional[bool] = Field(default=None, description="Require all required config fields")


class AddonRenderRequest(LumaRequestModel):
    images: Optional[Dict[str, str]] = Field(default=None, description="Per-service container image overrides")
    forSourceBuild: Optional[bool] = Field(default=None, description="Render compose manifests for local source build")


class AddonApplyRequest(LumaRequestModel):
    preset: Optional[str] = Field(default=None, description="Deployment preset identifier")
    config: Optional[Dict[str, Any]] = Field(default=None, description="Addon configuration fields")
    secrets: Optional[Dict[str, Any]] = Field(default=None, description="Scoped secrets to store before applying")
    inherit: Optional[Dict[str, Any]] = Field(default=None, description="Saved Control credentials to inherit")
    rotateSecrets: Optional[List[str]] = Field(default=None, description="Auto-generated secret names to rotate")
    images: Optional[Dict[str, str]] = Field(default=None, description="Per-service container image overrides")
    origin: Optional[str] = Field(default=None, description="Deployment origin ('addon', 'dashboard', or 'cli')")


class AddonUninstallRequest(LumaRequestModel):
    purgeSecrets: Optional[bool] = Field(default=None, description="Also delete scoped secrets for this addon")
    deleteStorage: Optional[bool] = Field(default=None, description="Also remove persistent volumes for the addon stack")


# ---------------------------------------------------------------------------
# Independent Builder
# ---------------------------------------------------------------------------


class BuilderSubmitRequest(LumaRequestModel):
    repoUrl: Optional[str] = Field(default=None, description="Git repository URL to build")
    providerId: Optional[str] = Field(default=None, description="Saved Control git provider identifier")
    repository: Optional[str] = Field(default=None, description="Owner/repository slug for the git provider")
    ref: Optional[str] = Field(default=None, description="Git branch, tag, or commit ref")
    region: Optional[str] = Field(default=None, description="Target deployment region")
    exposure: Optional[str] = Field(default=None, description="Target service exposure mode")
    domain: Optional[str] = Field(default=None, description="Public domain for deployment")
    port: Optional[int] = Field(default=None, description="Application container port")
    manifest: Optional[str] = Field(default=None, description="Inline Luma manifest YAML")
    platform: Optional[str] = Field(default=None, description="Target container platform")
    context: Optional[str] = Field(default=None, description="Docker build context path")
    dockerfile: Optional[str] = Field(default=None, description="Dockerfile path relative to context")
    envSecrets: Optional[Dict[str, Any]] = Field(default=None, description="Runtime environment secrets")


# ---------------------------------------------------------------------------
# Settings: Secrets, Regions, DNS Zones, Registries, Git Providers, CDN
# ---------------------------------------------------------------------------


class SecretSetRequest(LumaRequestModel):
    name: Optional[str] = Field(default=None, description="Environment variable secret name")
    value: Optional[Any] = Field(default=None, description="Non-empty secret value")
    scope: Optional[str] = Field(default=None, description="Optional application/stack scope")


class SecretRemoveRequest(LumaRequestModel):
    name: Optional[str] = Field(default=None, description="Environment variable secret name to remove")
    scope: Optional[str] = Field(default=None, description="Optional application/stack scope")


class RegionCreateRequest(LumaRequestModel):
    name: Optional[str] = Field(default=None, description="Custom region identifier")
    egress: Optional[str] = Field(default=None, description="Egress mode ('direct' or 'proxy')")
    exposures: Optional[List[str]] = Field(default=None, description="Allowed exposure modes for the region")


class RegionUpdateRequest(LumaRequestModel):
    name: Optional[str] = Field(default=None, description="Custom region identifier to update")
    egress: Optional[str] = Field(default=None, description="Updated egress mode")
    exposures: Optional[List[str]] = Field(default=None, description="Updated allowed exposure modes")


class RegionRemoveRequest(LumaRequestModel):
    name: Optional[str] = Field(default=None, description="Custom region identifier to remove")


class DnsZoneAddRequest(LumaRequestModel):
    name: Optional[str] = Field(default=None, description="Registered domain name (e.g. example.com)")
    provider: Optional[str] = Field(default=None, description="DNS provider ('cloudflare', 'alidns', or 'dnspod')")
    zoneId: Optional[str] = Field(default=None, description="Optional provider zone ID")
    sameAs: Optional[str] = Field(default=None, description="Reuse credentials from an existing zone")
    credentials: Optional[Dict[str, Any]] = Field(default=None, description="Provider credential key-value map")
    update: Optional[bool] = Field(default=None, description="Update an existing zone entry")


class DnsZoneCheckRequest(LumaRequestModel):
    name: Optional[str] = Field(default=None, description="DNS zone name to verify")


class DnsZoneRemoveRequest(LumaRequestModel):
    name: Optional[str] = Field(default=None, description="DNS zone name to remove")


class RegistrySetRequest(LumaRequestModel):
    host: Optional[str] = Field(default=None, description="Container registry hostname")
    serverAddress: Optional[str] = Field(default=None, description="Alternate registry server address field")
    username: Optional[str] = Field(default=None, description="Registry username")
    password: Optional[Any] = Field(default=None, description="Registry password or token")


class RegistryRemoveRequest(LumaRequestModel):
    host: Optional[str] = Field(default=None, description="Container registry hostname to remove")


class GitProviderSetRequest(LumaRequestModel):
    type: Optional[str] = Field(default=None, description="Git provider type ('github' or 'gitea')")
    account: Optional[str] = Field(default=None, description="Account label for this provider")
    username: Optional[str] = Field(default=None, description="Optional clone/API username")
    token: Optional[str] = Field(default=None, description="Personal access token")
    baseUrl: Optional[str] = Field(default=None, description="Provider API base URL")
    cloneBaseUrl: Optional[str] = Field(default=None, description="Optional Git clone base URL")


class GitProviderRemoveRequest(LumaRequestModel):
    id: Optional[str] = Field(default=None, description="Provider identifier (e.g. github:octocat)")
    type: Optional[str] = Field(default=None, description="Provider type when id is omitted")
    account: Optional[str] = Field(default=None, description="Provider account when id is omitted")


class CdnCheckRequest(LumaRequestModel):
    domain: Optional[str] = Field(default=None, description="Optional CDN domain context")


class CdnPurgeRequest(LumaRequestModel):
    domain: Optional[str] = Field(default=None, description="CDN-managed domain to purge")
    paths: Optional[List[str]] = Field(default=None, description="Optional list of URL paths to purge")


class CdnRemoveRequest(LumaRequestModel):
    domain: Optional[str] = Field(default=None, description="Unused CDN domain to remove")


for _cls in list(globals().values()):
    if isinstance(_cls, type) and issubclass(_cls, BaseModel) and _cls is not BaseModel:
        _cls.model_rebuild()
