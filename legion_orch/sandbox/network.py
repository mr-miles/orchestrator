"""Network access for environments: endpoints, the policy as attached to one sandbox profile,
and the ``network_policies`` entry it becomes in that sandbox's own policy.

A library policy carries no credential, so it is not an OpenShell provider: it is rendered into
the policy of each sandbox the profile applies to (:func:`merge_network`), open to the profile's
binaries that brought it. Running sandboxes keep the policy they were created with.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

NET_KEY_PREFIX = "env_"        # network_policies keys of environment policies


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


def policy_entry(policy: Policy) -> tuple[str, dict[str, Any]]:
    """The policy's ``network_policies`` key and entry in a sandbox policy."""
    key = NET_KEY_PREFIX + re.sub(r"[^A-Za-z0-9_]", "_", policy.name)
    return key, {"name": f"env-{policy.name}",
                 "endpoints": [e.model_dump(exclude_none=True) for e in policy.endpoints],
                 "binaries": [{"path": b} for b in policy.binaries]}


def merge_network(doc: dict[str, Any], network: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Add environment policies to a rendered sandbox policy. A key the template already uses
    is an error rather than a silent replacement of the template's rule."""
    if not network:
        return doc
    rules = doc.get("network_policies") or {}
    if clash := sorted(set(rules) & set(network)):
        raise ValueError(f"environment network policies {clash} clash with the sandbox policy "
                         "template's network_policies keys")
    doc["network_policies"] = rules | network
    return doc
