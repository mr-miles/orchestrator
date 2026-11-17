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
    "sandbox_resources": "sandbox-resources.template.yaml",
}

# name -> description. Keep policies/README.md in sync (tests check both directions).
VOCABULARY: dict[str, str] = {
    # controller endpoints (from server.*)
    "GIT_HOST": "git listener host as seen from sandboxes (server.public_host)",
    "GIT_PORT": "git listener port (server.git_port)",
    "MCP_HOST": "MCP listener host as seen from sandboxes (server.public_host)",
    "MCP_PORT": "MCP listener port (server.mcp_port)",
    "MCP_PATH": "MCP endpoint path, e.g. /mcp",
    "MCP_VERSIONS": "MCP protocol revisions the gateway accepts",
    "MCP_MAX_BODY_BYTES": "largest MCP request the gateway inspects (server.mcp_max_body_bytes)",
    "MCP_TOOLS": "tool names the controller's MCP server registers (the tools/call allowlist)",
    "GIT_PUSH_DENY_RULES": "deny rules for git-receive-pack: [] for roles that push, else a POST deny",
    "TOKEN_ENDPOINT": "the controller's OAuth token endpoint (server.token_port, /oauth/token)",
    # identity (from identity.*)
    "TOKEN_AUDIENCE": "audience requested for controller access tokens",
    "SVID_AUDIENCE": "audience of the sandbox JWT-SVID presented to the token endpoint",
    "SCOPE": "OAuth scope requested for controller access tokens",
    "TOKEN_CACHE_TTL": "seconds the gateway may cache a controller access token",
    "PROVIDER": "OpenShell provider name for controller access (identity.provider)",
    "PROFILE_ID": "OpenShell provider profile id / provider type (legion-controller)",
    # OpenShell placement and images (from sandbox.*)
    "WORKSPACE": "OpenShell workspace (sandbox.workspace)",
    "AGENT_IMAGE": "image for agent sandboxes: its environment image, else sandbox.image "
                   "(null: gateway default)",
    "VERIFIER_IMAGE": "image for verifier sandboxes: its environment image, else "
                      "sandbox.verifier_image or sandbox.image",
    "SANDBOX_PROVIDERS": "providers attached to agent sandboxes: PROVIDER + sandbox.providers",
    "AGENT_BINARIES": "policy binaries [{path}] of the agent CLI (sandbox.agent_binaries)",
    "GIT_BINARIES": "policy binaries [{path}] of git and its HTTP remote helpers",
    "PROFILE_BINARIES": "plain paths of agent and git binaries, for the provider profile",
    # per sandbox (from the controller at creation time)
    "SANDBOX_NAME": "readable sandbox name: lg-<first name> (agents), lgv-<first name> (verifiers)",
    "POLICY_FILE": "path of the rendered sandbox policy",
    "PAYLOAD_UPLOAD": "upload spec <local payload dir>:<sandbox.root>",
    "SANDBOX_WRITABLE": "paths the sandbox may write: sandbox.root, /tmp, /dev/null",
    "RUN_AS_USER": "user the sandbox processes run as (sandbox.run_as_user)",
    "RUN_AS_GROUP": "group the sandbox processes run as (sandbox.run_as_group)",
    "CHANGE_ID": "change id the sandbox works for",
    "RUN_ID": "run id that created the sandbox (verifiers: the verifier name)",
    "ROLE": "role the sandbox serves (implementer, reviewer, ..., verifier)",
    "TASK_ID": "node / task id the sandbox serves (sanitised automatically in labels)",
    "VERIFIER_PROVIDERS": "providers attached to verifier sandboxes: PROVIDER",
    # environment resources (null when the spec leaves them out: driver defaults)
    "SANDBOX_CPU": "--cpu: vCPUs requested by the sandbox's environment profile",
    "SANDBOX_MEMORY": "--memory: memory requested by the sandbox's environment profile (<n>Mi)",
    "DRIVER_CONFIG": "--driver-config-json for the sandbox's disk (the rendered sandbox-resources "
                     "template)",
    "DISK_MIB": "disk (MiB) requested by the sandbox's environment profile",
}

MCP_VERSIONS = ["2025-06-18", "2025-11-25", "2026-07-28"]
PROFILE_ID = "legion-controller"


class TemplateError(ValueError):
    pass


def _host_port(url: str) -> tuple[str, int, str]:
    """Endpoint host, port and path. No ``tls`` field is rendered: OpenShell detects TLS itself
    and inspects either way (it accepts only an omitted value or ``skip``, which would turn off
    inspection and the bearer injection)."""
    u = urlparse(url)
    return (u.hostname or "host.openshell.internal", u.port or (443 if u.scheme == "https" else 80),
            u.path or "/")


def base_values(cfg: Config, *, mcp_tools: list[str] | None = None) -> dict[str, Any]:
    """Vocabulary values derived from configuration (no per-sandbox values)."""
    gh, gp, _ = _host_port(cfg.server.git_base_url)
    mh, mp, mpath = _host_port(cfg.server.mcp_url)
    ic, sc = cfg.identity, cfg.sandbox
    v: dict[str, Any] = {
        "GIT_HOST": gh, "GIT_PORT": gp,
        "MCP_HOST": mh, "MCP_PORT": mp, "MCP_PATH": mpath,
        "MCP_VERSIONS": MCP_VERSIONS, "MCP_MAX_BODY_BYTES": cfg.server.mcp_max_body_bytes,
        "TOKEN_ENDPOINT": cfg.server.token_url,
        "TOKEN_AUDIENCE": ic.token_audience, "SVID_AUDIENCE": ic.svid_audience, "SCOPE": ic.scope,
        "TOKEN_CACHE_TTL": max(30, min(ic.access_ttl_s // 2, 120)),
        "PROVIDER": ic.provider, "PROFILE_ID": PROFILE_ID, "WORKSPACE": sc.workspace,
        "AGENT_IMAGE": sc.image, "VERIFIER_IMAGE": sc.verifier_image or sc.image,
        "SANDBOX_PROVIDERS": list(dict.fromkeys([ic.provider, *sc.providers])),
        "VERIFIER_PROVIDERS": [ic.provider],
        "SANDBOX_CPU": None, "SANDBOX_MEMORY": None, "DRIVER_CONFIG": None,
        "SANDBOX_WRITABLE": [sc.root, "/tmp", "/dev/null"],
        "RUN_AS_USER": sc.run_as_user, "RUN_AS_GROUP": sc.run_as_group,
        "AGENT_BINARIES": [{"path": b} for b in sc.agent_binaries],
        "GIT_BINARIES": [{"path": b} for b in sc.git_binaries],
        "PROFILE_BINARIES": list(dict.fromkeys([*sc.agent_binaries, *sc.git_binaries])),
    }
    if mcp_tools is not None:
        v["MCP_TOOLS"] = list(mcp_tools)
    return v


def label_value(value: str) -> str:
    """OpenShell label values: ≤63 chars of alphanumerics, '-', '_', '.', alphanumeric at both ends."""
    v = re.sub(r"[^A-Za-z0-9._-]", ".", value)[:63]
    return re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", v)


def git_push_deny_rules(cfg: Config, push: bool) -> list[dict]:
    """Deny rules take precedence over allows in OpenShell, so the templates always allow
    git-receive-pack and roles that may not push get this deny."""
    return [] if push else [{"method": "POST", "path": "/git/*.git/git-receive-pack"}]


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
    ``--name x --provider a --provider b --label k=v --detach``. Label values are sanitised
    with :func:`label_value`; annotations keep exact values."""
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
                if key == "label":          # OpenShell label values have a restricted alphabet
                    item = label_value(str(item))
                args += [flag, f"{k}={item}"]
        else:
            args += [flag, str(val)]
    return args
