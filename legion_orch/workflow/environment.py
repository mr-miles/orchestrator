"""The environment library and spec resolution.

The library (``environment.library_dir``, default ``environments/``) holds reusable building
blocks, one YAML file each:

* ``tools/<name>.yaml``    – ``description``, ``mise: {<tool id>: <version>}``, optional ``apt``
  (system packages) and ``binaries`` (real paths, for network-policy matching);
* ``policies/<name>.yaml`` – ``description``, ``endpoints``, ``binaries``: one network policy,
  turned into a credentialless OpenShell provider profile.

Both use the formats of the ``environment`` schema's ``tool`` and ``policy`` definitions, so a
proposal in a spec (``proposed.tools`` / ``proposed.policies``) promotes to the library verbatim.
A spec composes profiles from library names and its own proposals (:func:`resolve`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from ..sandbox import Policy, SandboxEnv, Tool
from ..sandbox.environment import binary_paths, tool_env
from .model import EnvironmentBuild

KINDS = ("tools", "policies")
# Roles that run before the environment exists (they always use the default sandbox).
PRE_PROVISION_ROLES = {"intake", "planner", "environment", "critic"}
VERIFIER = "verifier"           # the deterministic verifier sandbox, mappable like a role


class LibraryError(Exception):
    pass


def _validator(schema: dict, kind: str) -> Draft202012Validator:
    d = "tool" if kind == "tools" else "policy"
    return Draft202012Validator({"$defs": schema["$defs"], "$ref": f"#/$defs/{d}"})


@dataclass
class Library:
    root: Path
    tools: dict[str, dict] = field(default_factory=dict)
    policies: dict[str, dict] = field(default_factory=dict)

    def entries(self, kind: str) -> dict[str, dict]:
        return self.tools if kind == "tools" else self.policies

    def catalogue(self) -> str:
        """Markdown list of the library for the environment agent's prompt."""
        lines = ["Tools (`environments/tools/`):"]
        for n, t in sorted(self.tools.items()):
            ver = ", ".join(f"{k}@{v}" for k, v in (t.get("mise") or {}).items())
            how = f"mise: {ver}" if ver else f"apt: {', '.join(t.get('apt', []))}"
            net = f"; brings network: {', '.join(t['network'])}" if t.get("network") else ""
            lines.append(f"- `{n}` – {t['description']} ({how}{net})")
        lines.append("\nNetwork policies (`environments/policies/`):")
        for n, p in sorted(self.policies.items()):
            hosts = ", ".join(f"{e['host']}:{e['port']}" for e in p["endpoints"])
            lines.append(f"- `{n}` – {p['description']} ({hosts})")
        if len(lines) == 2:
            return "(the library is empty)"
        return "\n".join(lines)


def load_library(root: Path, schema: dict) -> Library:
    """Load and validate every library file against the environment schema's definitions."""
    lib = Library(root=Path(root))
    for kind in KINDS:
        d = lib.root / kind
        if not d.is_dir():
            continue
        v = _validator(schema, kind)
        for p in sorted(d.glob("*.yaml")):
            try:
                body = yaml.safe_load(p.read_text())
            except yaml.YAMLError as e:
                raise LibraryError(f"{p}: invalid YAML: {e}") from None
            errors = sorted(v.iter_errors(body), key=lambda e: e.path)
            if errors:
                raise LibraryError(f"{p}: {errors[0].message}")
            if not Draft202012Validator({"$ref": "#/$defs/name", "$defs": schema["$defs"]}).is_valid(p.stem):
                raise LibraryError(f"{p}: library file names must be <lowercase-name>.yaml")
            lib.entries(kind)[p.stem] = body
    errors = check_library(lib)
    if errors:
        raise LibraryError(f"{lib.root}: " + "; ".join(errors))
    return lib


def entry_yaml(body: dict) -> str:
    return yaml.safe_dump(body, sort_keys=False)


# ---------------------------------------------------------------------------- resolution
@dataclass
class ResolvedProfile:
    name: str
    tools: list[Tool]
    policies: list[Policy]
    resources: dict[str, int]


def _tool(name: str, body: dict, version: str | None) -> tuple[Tool | None, str | None]:
    mise = dict(body.get("mise") or {})
    if version:
        if len(mise) != 1:
            return None, f"tool {name} bundles {len(mise)} mise tools; it has no single version to set"
        mise = {k: version for k in mise}
    binaries, err = binary_paths(mise, body.get("binaries", []))
    if err:
        return None, f"tool {name}: {err}"
    return Tool(name=name, description=body["description"], mise=mise,
                apt=tuple(body.get("apt", [])), binaries=binaries,
                env=tuple(sorted((body.get("env") or {}).items()))), None


def _network(pname: str, prof: dict, tools: list[Tool], tool_bodies: dict[str, dict],
             policies: dict[str, dict]) -> tuple[list[Policy], list[str]]:
    """A profile's network: every policy its tools declare (unless ``offline``) plus the
    profile's per-tool extras. Each policy allows exactly the binaries of the tools using it."""
    v: list[str] = []
    extra = prof.get("network") or {}
    names = {t.name for t in tools}
    v += [f"G-E1: profile {pname}: network extras for {t!r}, which is not one of its tools"
          for t in extra if t not in names]
    users: dict[str, list[Tool]] = {}
    for t in tools:
        declared = [] if prof.get("offline") else tool_bodies[t.name].get("network", [])
        for pol in dict.fromkeys([*declared, *extra.get(t.name, [])]):
            if pol not in policies:
                v.append(f"G-E1: profile {pname}: unknown network policy {pol!r} for tool "
                         f"{t.name} (not in the library or proposed)")
            else:
                users.setdefault(pol, []).append(t)
    out = []
    for name, ts in users.items():
        b = policies[name]
        pol = Policy(name=name, description=b["description"], endpoints=tuple(b["endpoints"]),
                     binaries=tuple(dict.fromkeys(x for t in ts for x in t.binaries)),
                     tools=tuple(t.name for t in ts))
        if not pol.binaries:
            v.append(f"G-E1: profile {pname}: network policy {name} is for tools "
                     f"{list(pol.tools)}, which declare no binaries; add them to the tool entries")
        out.append(pol)                  # still checked by the other guards (G-E2)
    return out, v


def check_library(lib: Library) -> list[str]:
    """Cross-entry rules: tools resolve, and the network they declare exists."""
    errors = []
    for n, body in lib.tools.items():
        _, err = _tool(n, body, None)
        if err:
            errors.append(err)
        missing = [p for p in body.get("network", []) if p not in lib.policies]
        if missing:
            errors.append(f"tool {n} needs unknown network policies {missing}")
    return errors


def resolve(spec: dict, lib: Library) -> tuple[dict[str, ResolvedProfile], list[str]]:
    """Profiles of a (schema-valid) spec resolved against the library and its proposals.
    Returns the profiles and G-E1 violations (unknown names, shadowing, bad versions)."""
    v: list[str] = []
    proposed = spec.get("proposed") or {}
    for kind in KINDS:
        for n in sorted(set(proposed.get(kind) or {}) & set(lib.entries(kind))):
            v.append(f"G-E1: proposed {kind[:-1]} {n!r} shadows the library entry of that name; "
                     "use the library entry or choose a new name")
    tools = lib.tools | (proposed.get("tools") or {})
    policies = lib.policies | (proposed.get("policies") or {})
    out: dict[str, ResolvedProfile] = {}
    for pname, prof in spec["profiles"].items():
        rp = ResolvedProfile(name=pname, tools=[], policies=[], resources=dict(prof.get("resources") or {}))
        for ref in prof["tools"]:
            name, _, version = ref.partition("@")
            if name not in tools:
                v.append(f"G-E1: profile {pname}: unknown tool {name!r} (not in the library or proposed)")
                continue
            t, err = _tool(name, tools[name], version or None)
            if err:
                v.append(f"G-E1: profile {pname}: {err}")
            elif t:
                rp.tools.append(t)
        rp.policies, nv = _network(pname, prof, rp.tools, tools, policies)
        v += nv
        _, conflicts = tool_env(rp.tools)
        v += [f"G-E1: profile {pname}: conflicting tool environment: {c}" for c in conflicts]
        out[pname] = rp
    for role, pname in spec["roles"].items():
        if pname not in spec["profiles"]:
            v.append(f"G-E1: role {role} mapped to unknown profile {pname!r}")
    return out, v


def used_proposals(spec: dict) -> dict[str, list[str]]:
    """Proposed entries a profile actually uses (what promotion and the UI highlight): proposed
    tools in a profile, and proposed policies a used proposed tool needs or a profile adds."""
    proposed = spec.get("proposed") or {}
    ptools, ppols = proposed.get("tools") or {}, proposed.get("policies") or {}
    used_t = {r.partition("@")[0] for p in spec["profiles"].values() for r in p["tools"]}
    used_p = {n for p in spec["profiles"].values() for ns in (p.get("network") or {}).values()
              for n in ns}
    used_p |= {n for t in used_t & set(ptools) for n in ptools[t].get("network", [])}
    return {"tools": sorted(set(ptools) & used_t), "policies": sorted(set(ppols) & used_p)}


def proposal(spec: dict, kind: str, name: str) -> dict[str, Any] | None:
    return ((spec.get("proposed") or {}).get(kind) or {}).get(name)


def sandbox_env(build: EnvironmentBuild | None, role: str) -> SandboxEnv | None:
    """The environment of a sandbox serving ``role`` (or the verifier), if the spec maps it."""
    if build is None or role not in build.roles:
        return None
    pb = build.profiles.get(build.roles[role])
    if pb is None:
        return None
    return SandboxEnv(image=pb.verifier_image if role == VERIFIER else pb.image,
                      providers=list(pb.providers), driver_config=pb.driver_config)
