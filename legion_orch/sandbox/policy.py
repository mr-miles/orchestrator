"""Sandbox policies, rendered from ``policies/{agent,verifier}-policy.template.yaml``.

They carry no rule for the controller: its MCP endpoint and git remote reach a sandbox through
the attached provider profiles (``legion-mcp``; ``legion-git`` or ``legion-git-push``, see
:mod:`.profile`), whose endpoints OpenShell adds to the effective policy. The controller adds the
sandbox environment's network policies (``env_*``).
"""
from __future__ import annotations

import yaml

from ..config import Config
from .network import merge_network
from .templates import base_values, render


def render_policy(cfg: Config, *, agent: bool, network: dict[str, dict] | None = None) -> str:
    """The sandbox policy; ``network`` adds the sandbox environment's network policies."""
    doc = render(cfg, "agent_policy" if agent else "verifier_policy", base_values(cfg))
    return yaml.safe_dump(merge_network(doc, network or {}), sort_keys=False)
