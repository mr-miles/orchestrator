"""One-time OpenShell workspace setup: the controller's provider profiles and their providers.

Controller access is one provider profile per endpoint, each with the SPIFFE token grant that
injects a task-scoped bearer into requests to that endpoint only:

* ``legion-mcp`` (``policies/legion-mcp-profile.template.yaml``) – the MCP endpoint, for agents;
* ``legion-a2a`` (``policies/legion-a2a-profile.template.yaml``) – the A2A endpoint, for agents;
* ``legion-git`` and ``legion-git-push`` (``policies/legion-git-profile.template.yaml``) – the git
  remote, read-only (a ``git-receive-pack`` deny) and read-write.

Each gets a provider of the same name (``policies/legion-provider.template.yaml``). Attached
profiles contribute their endpoints to a sandbox's effective policy, so the sandbox policies
carry no rule for the controller.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ..config import Config
from .runner import openshell_cli
from .templates import (
    A2A_PROFILE,
    CONTROLLER_PROFILES,
    GIT_PROFILE,
    GIT_PUSH_PROFILE,
    MCP_PROFILE,
    base_values,
    git_push_deny_rules,
    render,
    spec_to_args,
)


def provider_profiles(cfg: Config, mcp_tools: list[str]) -> dict[str, dict[str, Any]]:
    """The controller's provider profiles, by id."""
    values = base_values(cfg, mcp_tools=mcp_tools)
    return {
        MCP_PROFILE: render(cfg, "mcp_profile", values | {"PROFILE_ID": MCP_PROFILE}),
        A2A_PROFILE: render(cfg, "a2a_profile", values | {"PROFILE_ID": A2A_PROFILE}),
        **{pid: render(cfg, "git_profile", values | {"PROFILE_ID": pid,
                                                    "GIT_PUSH_DENY_RULES": git_push_deny_rules(push)})
           for pid, push in ((GIT_PROFILE, False), (GIT_PUSH_PROFILE, True))},
    }


def profile_file(out_dir: Path | str, pid: str) -> Path:
    return Path(out_dir) / f"{pid}.yaml"


def _provider_create(cfg: Config, pid: str) -> list[str]:
    spec = render(cfg, "provider", base_values(cfg) | {"PROFILE_ID": pid})
    return [*openshell_cli(cfg), "provider", "create", *spec_to_args(spec)]


def setup_commands(cfg: Config, out_dir: Path | str) -> list[list[str]]:
    """Run once per workspace (not per sandbox): import each profile, create its provider."""
    cli = openshell_cli(cfg)
    return [step for pid in CONTROLLER_PROFILES
            for step in ([*cli, "profile", "import", "-f", str(profile_file(out_dir, pid))],
                         _provider_create(cfg, pid))]


# run(command, probe): probes (export, get) are captured; the real steps show their output
Run = Callable[[list[str], bool], subprocess.CompletedProcess]


def setup_workspace(cfg: Config, profiles: dict[str, dict[str, Any]], out_dir: Path, run: Run,
                    say: Callable[[str], None] = print) -> int:
    """Apply the setup idempotently, per profile: import it, or update it if the workspace
    already has it (``profile update`` needs the stored ``resource_version``); create its
    provider unless it exists. Returns the first failing step's exit code."""
    cli = openshell_cli(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    for pid, profile in profiles.items():
        out = profile_file(out_dir, pid)
        current = run([*cli, "profile", "export", pid, "-o", "json"], True)
        if current.returncode == 0:
            version = json.loads(current.stdout)["resource_version"]
            out.write_text(yaml.safe_dump(profile | {"resource_version": version}, sort_keys=False))
            step = [*cli, "profile", "update", "-f", str(out), pid]
        else:
            out.write_text(yaml.safe_dump(profile, sort_keys=False))
            step = [*cli, "profile", "import", "-f", str(out)]
        say("$ " + " ".join(step))
        if (rc := run(step, False).returncode) != 0:
            return rc
        if run([*cli, "provider", "get", pid], True).returncode == 0:
            say(f"provider {pid} exists; kept")
            continue
        step = _provider_create(cfg, pid)
        say("$ " + " ".join(step))
        if (rc := run(step, False).returncode) != 0:
            return rc
    return 0


__all__ = ["CONTROLLER_PROFILES", "profile_file", "provider_profiles", "setup_commands",
           "setup_workspace"]
