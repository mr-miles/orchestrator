"""Network access for environments: endpoints, the policy as attached to one sandbox profile,
and the credentialless OpenShell provider profile that carries it.

Attached providers contribute their endpoints to the sandbox's effective policy, so the sandbox
policy templates never change. Provider ids are content-addressed: an edited policy (or a tool
whose binaries changed) is a new provider, and running sandboxes keep the one they started with.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

NET_PREFIX = "legion-net-"


class Endpoint(BaseModel):
    """One HTTPS endpoint, inspected by the gateway (``protocol: rest``)."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    host: str
    port: int
    path: str | None = None
    protocol: Literal["rest"] = "rest"
    enforcement: Literal["enforce", "audit"] = "enforce"
    access: Literal["read-only", "read-write", "full"] = "read-only"


@dataclass(frozen=True)
class Policy:
    """A network policy as attached to one profile: ``binaries`` are those of the tools that
    brought it (or that the profile added it for) – nothing else in the sandbox may use it."""
    name: str
    description: str
    endpoints: tuple[Endpoint, ...]
    binaries: tuple[str, ...]
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class NetworkProvider:
    """The provider profile (and provider of the same name) that carries one :class:`Policy`."""
    id: str
    body: dict[str, Any]

    @classmethod
    def of(cls, policy: Policy) -> NetworkProvider:
        body = {"display_name": f"legion network: {policy.name}", "description": policy.description,
                "category": "other", "credentials": [],
                "endpoints": [e.model_dump(exclude_none=True) for e in policy.endpoints],
                "binaries": list(policy.binaries)}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:8]
        return cls(id=f"{NET_PREFIX}{policy.name}-{digest}", body=body)

    def profile(self) -> dict[str, Any]:
        """The document ``openshell profile import`` takes."""
        return {"id": self.id} | self.body
