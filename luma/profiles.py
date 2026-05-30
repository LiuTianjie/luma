from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class Profile:
    name: str
    roles: List[str]
    labels: Dict[str, str]
    description: str


PROFILES: Dict[str, Profile] = {
    "single-node": Profile(
        name="single-node",
        roles=["swarm-manager", "edge", "egress"],
        labels={"region": "cn", "ingress": "true", "egress": "true"},
        description="One public server running Swarm, Traefik, and egress gateway.",
    ),
    "cn-edge": Profile(
        name="cn-edge",
        roles=["swarm-manager", "edge"],
        labels={"region": "cn", "ingress": "true"},
        description="Domestic public edge with Traefik and Swarm manager.",
    ),
    "egress-gateway": Profile(
        name="egress-gateway",
        roles=["egress"],
        labels={"egress": "true"},
        description="Outbound proxy gateway for image pulls and selected services.",
    ),
    "home-node": Profile(
        name="home-node",
        roles=["home"],
        labels={"region": "home"},
        description="Home/private node for internal tools, relay, or tunnel services.",
    ),
    "global-worker": Profile(
        name="global-worker",
        roles=["global-worker"],
        labels={"region": "global"},
        description="Overseas/external-network worker node.",
    ),
}


def get_profile(name: str) -> Profile:
    return PROFILES[name]
