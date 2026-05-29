from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .io import load_yaml


@dataclass(frozen=True)
class LumaConfig:
    raw: Dict[str, Any]
    path: Optional[Path]

    @property
    def stack_root(self) -> Path:
        value = self.raw.get("defaults", {}).get("stackRoot", "stacks")
        return Path(value)

    @property
    def public_network(self) -> str:
        return str(self.raw.get("defaults", {}).get("publicNetwork", "public"))

    @property
    def routes_root(self) -> Path:
        value = self.raw.get("defaults", {}).get("routesRoot", "routes")
        return Path(value)

    @property
    def cert_resolver(self) -> str:
        return str(self.raw.get("defaults", {}).get("certResolver", "letsencrypt"))

    @property
    def entrypoint(self) -> str:
        return str(self.raw.get("defaults", {}).get("entrypoint", "websecure"))

    @property
    def dns(self) -> Dict[str, Any]:
        return dict(self.raw.get("dns") or {})

    @property
    def portainer(self) -> Dict[str, Any]:
        return dict(self.raw.get("portainer") or {})

    @property
    def git(self) -> Dict[str, Any]:
        return dict(self.raw.get("git") or {})


def load_config(path: Optional[Path]) -> LumaConfig:
    if path:
        return LumaConfig(load_yaml(path), path)
    default = Path("luma.yaml")
    if default.exists():
        return LumaConfig(load_yaml(default), default)
    return LumaConfig({}, None)
