"""Sandbox network policies, rendered from ``policies/{agent,verifier}-policy.template.yaml``.

The controller exposes two endpoints to sandboxes, on different ports because OpenShell does not
allow an MCP endpoint to share a host and port with another protocol:

* **MCP** (``protocol: mcp``): the gateway parses every JSON-RPC request and allows only the MCP
  handshake methods and ``tools/call`` for the tools the controller's MCP server registers
  (``mcp_tools``, see legion_orch.server.mcp.MCP_TOOLS).
* **git** (``protocol: rest``): fetch; push only for roles that may push (others get a deny rule,
  which takes precedence). Verifiers get fetch only.
"""
from __future__ import annotations

import yaml

from ..config import Config
from .templates import base_values, git_push_deny_rules, render


def render_policy(cfg: Config, *, agent: bool, push: bool, mcp_tools: list[str]) -> str:
    values = base_values(cfg, mcp_tools=mcp_tools) | {
        "GIT_PUSH_DENY_RULES": git_push_deny_rules(cfg, push and agent)}
    doc = render(cfg, "agent_policy" if agent else "verifier_policy", values)
    return yaml.safe_dump(doc, sort_keys=False)
