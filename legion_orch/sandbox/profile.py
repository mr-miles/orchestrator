"""One-time OpenShell workspace setup, rendered from ``policies/legion-provider-profile.template.yaml``
(the SPIFFE token grant injected into controller requests only) and
``policies/legion-provider.template.yaml`` (the provider every sandbox attaches)."""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

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


# run(command, probe): probes (export, get) are captured; the real steps show their output
Run = Callable[[list[str], bool], subprocess.CompletedProcess]


def setup_workspace(cfg: Config, profile: dict[str, Any], out: Path, run: Run,
                    say: Callable[[str], None] = print) -> int:
    """Apply the setup idempotently: import the profile, or update it if the workspace already
    has one (``profile update`` needs the stored ``resource_version``); create the provider
    unless it exists. Returns the first failing step's exit code."""
    cli = openshell_cli(cfg)
    current = run([*cli, "profile", "export", PROFILE_ID, "-o", "json"], True)
    if current.returncode == 0:
        version = json.loads(current.stdout)["resource_version"]
        out.write_text(yaml.safe_dump(profile | {"resource_version": version}, sort_keys=False))
        step = [*cli, "profile", "update", "-f", str(out), PROFILE_ID]
    else:
        out.write_text(yaml.safe_dump(profile, sort_keys=False))
        step = [*cli, "profile", "import", "-f", str(out)]
    say("$ " + " ".join(step))
    if (rc := run(step, False).returncode) != 0:
        return rc
    if run([*cli, "provider", "get", PROFILE_ID], True).returncode == 0:
        say(f"provider {PROFILE_ID} exists; kept")
        return 0
    step = setup_commands(cfg, str(out))[1]
    say("$ " + " ".join(step))
    return run(step, False).returncode


__all__ = ["PROFILE_ID", "provider_profile", "setup_commands", "setup_workspace"]
