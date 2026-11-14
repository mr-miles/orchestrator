"""Templates for everything legion-orch creates in OpenShell (``policies/*.template.yaml``).

One vocabulary of ``${NAME}`` placeholders is shared by all templates (:data:`VOCABULARY`, also
documented in ``policies/README.md``). Every value is substituted **JSON-encoded**, which is always
valid YAML: strings are quoted, numbers bare, lists ``[...]``, maps ``{...}``, ``null`` for unset.
A placeholder that is not in the vocabulary, or has no value for this render, is an error.

Objects that OpenShell creates from a file (policies, provider profiles) are rendered to that file.
Objects created only through CLI flags (sandboxes, providers) use a *spec* template whose keys are
flag names, converted by :func:`spec_to_args`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from ..config import Config

PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

TEMPLATES = {
    "agent_policy": "agent-policy.template.yaml",
    "verifier_policy": "verifier-policy.template.yaml",
    "agent_sandbox": "agent-sandbox.template.yaml",
    "verifier_sandbox": "verifier-sandbox.template.yaml",
    "provider_profile": "legion-provider-profile.template.yaml",
    "provider": "legion-provider.template.yaml",
}

# name -> description. Keep policies/README.md in sync (tests check both directions).
VOCABULARY: dict[str, str] = {
    # controller endpoints (from server.*)
    "CONTROLLER_HOST": "controller host as seen from sandboxes (server.public_url)",
    "CONTROLLER_PORT": "controller port for git and /oauth/token (server.public_url)",
    "CONTROLLER_TLS": '"none" for plain HTTP endpoints, "" for TLS',
    "MCP_HOST": "MCP endpoint host (server.mcp_url)",
    "MCP_PORT": "MCP endpoint port (server.mcp_port / server.mcp_url)",
    "MCP_PATH": "MCP endpoint path, e.g. /mcp",
    "MCP_TLS": '"none" for a plain HTTP MCP endpoint, "" for TLS',
    "MCP_VERSIONS": "MCP protocol revisions the gateway accepts",
    "MCP_TOOLS": "tool names the controller's MCP server registers (the tools/call allowlist)",
    "GIT_PATH_GLOB": "git endpoint path pattern, /git/<repo>.git/**",
    "GIT_INFO_REFS": "git ref advertisement path, /git/<repo>.git/info/refs",
    "GIT_UPLOAD_PACK": "git fetch path, /git/<repo>.git/git-upload-pack",
    "GIT_RECEIVE_PACK": "git push path, /git/<repo>.git/git-receive-pack",
    "GIT_PUSH_DENY_RULES": "deny rules for git-receive-pack: [] for roles that push, else a POST deny",
    "TOKEN_ENDPOINT": "the controller's OAuth token endpoint (<public_url>/oauth/token)",
    # identity (from identity.*)
    "TOKEN_AUDIENCE": "audience requested for controller access tokens",
    "SVID_AUDIENCE": "audience of the sandbox JWT-SVID presented to the token endpoint",
    "SCOPE": "OAuth scope requested for controller access tokens",
    "TOKEN_CACHE_TTL": "seconds the gateway may cache a controller access token",
    "PROVIDER": "OpenShell provider name for controller access (identity.provider)",
    "PROFILE_ID": "OpenShell provider profile id / provider type (legion-controller)",
    # OpenShell placement and images (from sandbox.*)
    "WORKSPACE": "OpenShell workspace (sandbox.workspace)",
    "AGENT_IMAGE": "image for agent sandboxes (sandbox.image), null for the gateway default",
    "VERIFIER_IMAGE": "image for verifier sandboxes (sandbox.verifier_image, else sandbox.image)",
    "SANDBOX_PROVIDERS": "providers attached to agent sandboxes: PROVIDER + sandbox.providers",
    "AGENT_BINARIES": "policy binaries [{path}] of the agent CLI (sandbox.agent_binaries)",
    "GIT_BINARIES": "policy binaries [{path}] of git and its HTTP remote helpers",
    "PROFILE_BINARIES": "plain paths of agent and git binaries, for the provider profile",
    # per sandbox (from the controller at creation time)
    "SANDBOX_NAME": "sandbox name (lg-<change>-<run>)",
    "POLICY_FILE": "path of the rendered sandbox policy",
    "PAYLOAD_UPLOAD": "upload spec <local payload dir>:/sandbox",
    "CHANGE_ID": "change id the sandbox works for",
    "RUN_ID": "run id that created the sandbox (verifiers: the verifier name)",
    "ROLE": "role the sandbox serves (implementer, reviewer, ..., verifier)",
    "TASK_ID": "node / task id the sandbox serves",
}

GIT_BINARIES = ["/usr/bin/git", "/usr/lib/git-core/git-remote-http",
                "/usr/lib/git-core/git-remote-https"]
MCP_VERSIONS = ["2025-06-18", "2025-11-25", "2026-07-28"]
PROFILE_ID = "legion-controller"


class TemplateError(ValueError):
    pass


def _host_port(url: str) -> tuple[str, int, str, str]:
    u = urlparse(url)
    return (u.hostname or "host.openshell.internal", u.port or (443 if u.scheme == "https" else 80),
            u.path or "/", "none" if u.scheme == "http" else "")


def base_values(cfg: Config, *, mcp_tools: list[str] | None = None) -> dict[str, Any]:
    """Vocabulary values derived from configuration (no per-sandbox values)."""
    ch, cp, _, ctls = _host_port(cfg.server.public_url)
    mh, mp, mpath, mtls = _host_port(cfg.server.mcp_url)
    ic, sc = cfg.identity, cfg.sandbox
    v: dict[str, Any] = {
        "CONTROLLER_HOST": ch, "CONTROLLER_PORT": cp, "CONTROLLER_TLS": ctls,
        "MCP_HOST": mh, "MCP_PORT": mp, "MCP_PATH": mpath, "MCP_TLS": mtls,
        "MCP_VERSIONS": MCP_VERSIONS,
        "GIT_PATH_GLOB": f"/git/{cfg.repo.name}.git/**",
        "GIT_INFO_REFS": f"/git/{cfg.repo.name}.git/info/refs",
        "GIT_UPLOAD_PACK": f"/git/{cfg.repo.name}.git/git-upload-pack",
        "GIT_RECEIVE_PACK": f"/git/{cfg.repo.name}.git/git-receive-pack",
        "TOKEN_ENDPOINT": f"{cfg.server.public_url.rstrip('/')}/oauth/token",
        "TOKEN_AUDIENCE": ic.token_audience, "SVID_AUDIENCE": ic.svid_audience, "SCOPE": ic.scope,
        "TOKEN_CACHE_TTL": max(30, min(ic.access_ttl_s // 2, 120)),
        "PROVIDER": ic.provider, "PROFILE_ID": PROFILE_ID, "WORKSPACE": sc.workspace,
        "AGENT_IMAGE": sc.image, "VERIFIER_IMAGE": sc.verifier_image or sc.image,
        "SANDBOX_PROVIDERS": list(dict.fromkeys([ic.provider, *sc.providers])),
        "AGENT_BINARIES": [{"path": b} for b in sc.agent_binaries],
        "GIT_BINARIES": [{"path": b} for b in GIT_BINARIES],
        "PROFILE_BINARIES": list(dict.fromkeys([*sc.agent_binaries, *GIT_BINARIES])),
    }
    if mcp_tools is not None:
        v["MCP_TOOLS"] = list(mcp_tools)
    return v


def git_push_deny_rules(cfg: Config, push: bool) -> list[dict]:
    """Deny rules take precedence over allows in OpenShell, so the templates always allow
    git-receive-pack and roles that may not push get this deny."""
    return [] if push else [{"method": "POST", "path": f"/git/{cfg.repo.name}.git/git-receive-pack"}]


def template_path(cfg: Config, key: str) -> Path:
    return cfg.resolve(cfg.sandbox.templates_dir) / TEMPLATES[key]


def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text))


# A placeholder must be a complete YAML value: JSON encoding cannot be concatenated with text.
EMBEDDED = re.compile(r"(?:[^\s:\[{,]\$\{[A-Z0-9_]+\})|(?:\$\{[A-Z0-9_]+\}[^\s,\]}#])")


def render_text(text: str, values: dict[str, Any], *, source: str = "template") -> str:
    for n, line in enumerate(text.splitlines(), 1):
        code = line.split(" #", 1)[0] if not line.lstrip().startswith("#") else ""
        if EMBEDDED.search(code):
            raise TemplateError(f"{source}:{n}: placeholders must be whole values, not part of a "
                                f"string: {line.strip()}")
    unknown = placeholders(text) - set(VOCABULARY)
    if unknown:
        raise TemplateError(f"{source}: unknown placeholders {sorted(unknown)}")
    missing = placeholders(text) - set(values)
    if missing:
        raise TemplateError(f"{source}: no value for {sorted(missing)}")
    return PLACEHOLDER.sub(lambda m: json.dumps(values[m.group(1)]), text)


def render(cfg: Config, key: str, values: dict[str, Any]) -> dict[str, Any]:
    path = template_path(cfg, key)
    try:
        doc = yaml.safe_load(render_text(path.read_text(), values, source=path.name))
    except yaml.YAMLError as e:
        raise TemplateError(f"{path.name}: rendered YAML is invalid: {e}") from None
    if not isinstance(doc, dict):
        raise TemplateError(f"{path.name}: must render to a mapping")
    return doc


def spec_to_args(spec: dict[str, Any]) -> list[str]:
    """``{name: x, provider: [a, b], label: {k: v}, detach: true, from: null}`` →
    ``--name x --provider a --provider b --label k=v --detach``."""
    args: list[str] = []
    for key, val in spec.items():
        flag = "--" + key.replace("_", "-")
        if val is None or val is False:
            continue
        if val is True:
            args.append(flag)
        elif isinstance(val, list):
            for item in val:
                if item is not None:
                    args += [flag, str(item)]
        elif isinstance(val, dict):
            for k, item in val.items():
                args += [flag, f"{k}={item}"]
        else:
            args += [flag, str(val)]
    return args
