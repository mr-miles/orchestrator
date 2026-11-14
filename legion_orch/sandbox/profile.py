"""One-time OpenShell workspace setup, rendered from ``policies/legion-provider-profile.template.yaml``
(the SPIFFE token grant injected into controller requests only) and
``policies/legion-provider.template.yaml`` (the provider every sandbox attaches)."""
from __future__ import annotations

from typing import Any

from ..config import Config
from .runner import openshell_cli
from .templates import PROFILE_ID, base_values, render, spec_to_args


def provider_profile(cfg: Config, mcp_tools: list[str]) -> dict[str, Any]:
    return render(cfg, "provider_profile", base_values(cfg, mcp_tools=mcp_tools))


def setup_commands(cfg: Config, profile_file: str) -> list[list[str]]:
    """Run once per workspace (not per sandbox)."""
    cli = openshell_cli(cfg)
    provider = render(cfg, "provider", base_values(cfg))
    return [[*cli, "profile", "import", "-f", profile_file],
            [*cli, "provider", "create", *spec_to_args(provider)]]


__all__ = ["PROFILE_ID", "provider_profile", "setup_commands"]
