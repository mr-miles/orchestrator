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

from pydantic import BaseModel, ConfigDict, model_validator

NET_KEY_PREFIX = "env_"        # network_policies keys of environment policies


class RuleMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    method: Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
    path: str


class Rule(BaseModel):
    """An explicit L7 allow rule (``rules: [{allow: {method, path}}]``)."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    allow: RuleMatch


class Endpoint(BaseModel):
    """One HTTPS endpoint, inspected by the gateway (``protocol: rest``). Requests are limited
    by an ``access`` preset (``read-only`` unless set) or by explicit ``rules``; OpenShell
    rejects both on one endpoint."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    host: str
    port: int
    path: str | None = None
    protocol: Literal["rest"] = "rest"
    enforcement: Literal["enforce", "audit"] = "enforce"
    access: Literal["read-only", "read-write", "full"] | None = None
    rules: tuple[Rule, ...] | None = None
    # keep %2F in path segments instead of rejecting it (npm scoped packages: /@scope%2fname);
    # the gateway honours it only for explicit rules, not for an access preset
    allow_encoded_slash: bool | None = None

    @model_validator(mode="after")
    def _rules_or_access(self) -> Endpoint:
        if self.rules is not None and self.access is not None:
            raise ValueError("an endpoint takes rules or access, not both")
        if self.rules is not None and not self.rules:
            raise ValueError("rules may not be empty")
        if self.allow_encoded_slash and self.rules is None:
            raise ValueError("allow_encoded_slash needs explicit rules (it does not apply to an "
                             "access preset)")
        return self

    def entry(self) -> dict[str, Any]:
        """The endpoint as rendered into a sandbox policy."""
        out = self.model_dump(mode="json", exclude_none=True)
        if self.rules is None:
            out["access"] = self.access or "read-only"
        return out


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
                 "endpoints": [e.entry() for e in policy.endpoints],
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
