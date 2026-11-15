"""Sandboxes: runners (OpenShell, local), bootstrap scripts, network policy and provider profile.

Knows nothing about the workflow or the MCP server; the MCP tool allowlist is passed in.
"""
from . import agentlog
from .names import (
    AGENT_PREFIX,
    MAX_NAME,
    VERIFIER_PREFIX,
    check_name,
    human_name,
    is_name_clash,
)
from .policy import render_policy
from .profile import PROFILE_ID, provider_profile, setup_commands, setup_workspace
from .runner import LocalRunner, OpenShellRunner, Runner, SandboxError, SandboxSpec, make_runner
from .scripts import RUN_AGENT_SH, VERIFY_SETUP_SH
from .templates import TEMPLATES, VOCABULARY, TemplateError, base_values, render, spec_to_args

__all__ = ["agentlog", "render_policy", "PROFILE_ID", "provider_profile", "setup_commands", "setup_workspace",
           "LocalRunner", "OpenShellRunner", "Runner", "SandboxError", "SandboxSpec", "make_runner",
           "RUN_AGENT_SH", "VERIFY_SETUP_SH", "TEMPLATES", "VOCABULARY", "TemplateError",
           "base_values", "render", "spec_to_args", "MAX_NAME", "check_name", "human_name",
           "is_name_clash", "AGENT_PREFIX", "VERIFIER_PREFIX"]
