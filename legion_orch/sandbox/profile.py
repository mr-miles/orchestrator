"""One-time OpenShell workspace setup: the legion-controller provider profile (SPIFFE token grant
injected into controller requests only)."""
from __future__ import annotations

from typing import Any

from ..config import Config
from .policy import GIT_BINARIES, git_endpoint, mcp_endpoint
from .runner import openshell_cli

PROFILE_ID = "legion-controller"


def provider_profile(cfg: Config, mcp_tools: list[str]) -> dict[str, Any]:
    """Workspace provider profile: SPIFFE token grant injected into controller requests only."""
    ic = cfg.identity
    base = cfg.server.public_url.rstrip("/")
    return {
        "id": PROFILE_ID,
        "display_name": "Legion orchestrator",
        "description": "Controller access (MCP + git) with a task-scoped token obtained from the "
                       "sandbox's SPIFFE JWT-SVID",
        "category": "other",
        "credentials": [{
            "name": "access_token",
            "description": "Short-lived controller token (client_credentials, jwt-spiffe assertion)",
            "required": False,
            "auth_style": "bearer",
            "header_name": "Authorization",
            "token_grant": {
                "grant_type": "client_credentials",
                "token_endpoint": f"{base}/oauth/token",
                "audience": ic.token_audience,
                "jwt_svid_audience": ic.svid_audience,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe",
                "scopes": [ic.scope],
                "cache_ttl_seconds": max(30, min(ic.access_ttl_s // 2, 120)),
            },
        }],
        "endpoints": [mcp_endpoint(cfg, mcp_tools), git_endpoint(cfg, push=True)],
        "binaries": list(dict.fromkeys(list(cfg.sandbox.agent_binaries) + GIT_BINARIES)),
    }


def setup_commands(cfg: Config, profile_file: str) -> list[list[str]]:
    """One-time setup of the configured workspace (not per sandbox): a workspace-scoped profile
    and provider, so every sandbox created in that workspace can attach it."""
    cli = openshell_cli(cfg)
    return [[*cli, "profile", "import", "-f", profile_file],
            [*cli, "provider", "create", "--name", cfg.identity.provider, "--type", PROFILE_ID,
             "--runtime-credentials"]]
