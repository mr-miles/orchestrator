"""The environment library and the environment spec, as typed models, and how a spec resolves.

The library (``environment.library_dir``, default ``environments/``) holds reusable building
blocks, one YAML file each:

* ``tools/<name>.yaml``    – :class:`ToolEntry`: how to install the tool (mise and/or apt), its
  binaries, the network policies it needs and its image environment – the one place each of
  these is written for the tool;
* ``policies/<name>.yaml`` – :class:`PolicyEntry`: endpoints only; tools say who uses them.

A spec (``plan/environment.json``, :class:`EnvironmentSpec`) composes profiles from library
tools, adds per-tool network extras and may propose entries in the same formats, so an approved
proposal promotes into the library verbatim (:func:`promote`).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict

from ..config import Config
from ..sandbox import Endpoint, Policy, Resources, Tool
from ..sandbox.images import install_dir, merged_env

# Roles that run before the environment exists (they always use the default sandbox).
PRE_PROVISION_ROLES = {"intake", "planner", "critic"}
VERIFIER = "verifier"           # the deterministic verifier sandbox, mappable like a role


class LibraryError(Exception):
    pass


class Kind(StrEnum):
    TOOL = "tool"
    POLICY = "policy"

    @property
    def plural(self) -> str:
        """Library folder and spec ``proposed`` key."""
        return "tools" if self is Kind.TOOL else "policies"


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolEntry(_Entry):
    description: str
    mise: dict[str, str] = {}
    apt: list[str] = []
    binaries: list[str] = []          # relative to the mise install dir, or absolute
    network: list[str] = []           # policies the tool needs to work
    env: dict[str, str] = {}


class PolicyEntry(_Entry):
    description: str
    endpoints: list[Endpoint]


class Profile(_Entry):
    tools: list[str]                                # "name" or "name@version"
    network: dict[str, list[str]] = {}              # extra policies per tool
    offline: bool = False                           # none of the tools' own network
    resources: Resources = Resources()


class Proposals(_Entry):
    tools: dict[str, ToolEntry] = {}
    policies: dict[str, PolicyEntry] = {}

    def entries(self, kind: Kind) -> dict[str, ToolEntry] | dict[str, PolicyEntry]:
        return self.tools if kind is Kind.TOOL else self.policies


class EnvironmentSpec(_Entry):
    """``plan/environment.json`` (shape enforced by the environment schema)."""
    summary: str
    profiles: dict[str, Profile]
    roles: dict[str, str]                           # role (or "verifier") -> profile
    proposed: Proposals = Proposals()
    rationale: str

    def used_proposals(self) -> dict[Kind, list[str]]:
        """Proposals a profile actually uses: proposed tools in a profile, and proposed policies
        a profile adds or a used proposed tool needs."""
        used_t = {r.partition("@")[0] for p in self.profiles.values() for r in p.tools}
        used_p = {n for p in self.profiles.values() for ns in p.network.values() for n in ns}
        used_p |= {n for t in used_t & set(self.proposed.tools)
                   for n in self.proposed.tools[t].network}
        return {Kind.TOOL: sorted(used_t & set(self.proposed.tools)),
                Kind.POLICY: sorted(used_p & set(self.proposed.policies))}


# ---------------------------------------------------------------------------- library
def binary_paths(mise: dict[str, str], binaries: list[str]) -> tuple[tuple[str, ...], str | None]:
    """A tool's binaries as absolute paths: relative ones are under its single mise tool's
    install directory. Returns the paths and an error for an entry that cannot be resolved."""
    if any(".." in b.split("/") for b in binaries):
        return (), f"binaries may not contain '..': {binaries}"
    rel = [b for b in binaries if not b.startswith("/")]
    if rel and len(mise) != 1:
        return (), (f"relative binaries {rel} need exactly one mise tool to resolve against "
                    f"(this entry has {len(mise)}); use absolute paths")
    base = install_dir(next(iter(mise))) if rel else ""
    return tuple(b if b.startswith("/") else f"{base}/{b}" for b in binaries), None


def resolve_tool(name: str, entry: ToolEntry, version: str | None) -> tuple[Tool | None, str | None]:
    mise = dict(entry.mise)
    if version:
        if len(mise) != 1:
            return None, f"tool {name} bundles {len(mise)} mise tools; it has no single version to set"
        mise = {k: version for k in mise}
    binaries, err = binary_paths(mise, entry.binaries)
    if err:
        return None, f"tool {name}: {err}"
    return Tool(name=name, description=entry.description, mise=mise, apt=tuple(entry.apt),
                binaries=binaries, env=tuple(sorted(entry.env.items()))), None


@dataclass
class Library:
    root: Path
    tools: dict[str, ToolEntry]
    policies: dict[str, PolicyEntry]

    def entries(self, kind: Kind) -> dict[str, ToolEntry] | dict[str, PolicyEntry]:
        return self.tools if kind is Kind.TOOL else self.policies

    @classmethod
    def load(cls, root: Path, schema: dict) -> Library:
        """Load every file, validated against the environment schema's ``tool`` / ``policy``
        definitions, then check the library as a whole."""
        lib = cls(root=Path(root), tools={}, policies={})
        name_ok = Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/name"}).is_valid
        for kind, model in ((Kind.TOOL, ToolEntry), (Kind.POLICY, PolicyEntry)):
            d = lib.root / kind.plural
            if not d.is_dir():
                continue
            v = Draft202012Validator({"$defs": schema["$defs"], "$ref": f"#/$defs/{kind.value}"})
            for p in sorted(d.glob("*.yaml")):
                try:
                    body = yaml.safe_load(p.read_text())
                except yaml.YAMLError as e:
                    raise LibraryError(f"{p}: invalid YAML: {e}") from None
                errors = sorted(v.iter_errors(body), key=lambda e: e.path)
                if errors:
                    raise LibraryError(f"{p}: {errors[0].message}")
                if not name_ok(p.stem):
                    raise LibraryError(f"{p}: library file names must be <lowercase-name>.yaml")
                lib.entries(kind)[p.stem] = model.model_validate(body)
        errors = lib.problems()
        if errors:
            raise LibraryError(f"{lib.root}: " + "; ".join(errors))
        return lib

    @classmethod
    def for_config(cls, cfg: Config, schemas: Any) -> Library:
        """The configured library (``schemas``: the asset SchemaRegistry); read fresh each time,
        since operators edit and promote into it."""
        if cfg.environment is None:
            return cls(root=Path("."), tools={}, policies={})
        return cls.load(cfg.resolve(cfg.environment.library_dir), schemas.get("environment").body)

    def problems(self) -> list[str]:
        """Cross-entry rules: tools resolve, and the network they declare exists."""
        errors = []
        for n, t in self.tools.items():
            _, err = resolve_tool(n, t, None)
            if err:
                errors.append(err)
            missing = [p for p in t.network if p not in self.policies]
            if missing:
                errors.append(f"tool {n} needs unknown network policies {missing}")
        return errors

    def catalogue(self) -> str:
        """Markdown list of the library for the planner's prompt."""
        if not self.tools and not self.policies:
            return "(the library is empty)"
        lines = ["Tools (`environments/tools/`):"]
        for n, t in sorted(self.tools.items()):
            ver = ", ".join(f"{k}@{v}" for k, v in t.mise.items())
            how = f"mise: {ver}" if ver else f"apt: {', '.join(t.apt)}"
            net = f"; brings network: {', '.join(t.network)}" if t.network else ""
            lines.append(f"- `{n}` – {t.description} ({how}{net})")
        lines.append("\nNetwork policies (`environments/policies/`):")
        for n, p in sorted(self.policies.items()):
            hosts = ", ".join(f"{e.host}:{e.port}" for e in p.endpoints)
            lines.append(f"- `{n}` – {p.description} ({hosts})")
        return "\n".join(lines)


def promote(lib: Library, spec: EnvironmentSpec, kind: Kind, name: str, source: str) -> Path:
    """Copy an approved spec's proposal into the library (never over an existing entry)."""
    entry = spec.proposed.entries(kind).get(name)
    if entry is None:
        raise LibraryError(f"{source} proposes no {kind} {name!r}")
    if name in lib.entries(kind):
        raise LibraryError(f"the library already has {kind} {name!r}; not overwritten")
    dest = lib.root / kind.plural / f"{name}.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = entry.model_dump(mode="json", exclude_defaults=True)
    dest.write_text(f"# Promoted from {source}\n" + yaml.safe_dump(body, sort_keys=False))
    return dest


# ---------------------------------------------------------------------------- resolution
@dataclass
class ResolvedProfile:
    name: str
    tools: list[Tool]
    policies: list[Policy]
    resources: Resources


def _network(prof: Profile, tools: list[Tool], tool_entries: dict[str, ToolEntry],
             policies: dict[str, PolicyEntry]) -> tuple[list[Policy], list[str]]:
    """Every policy the profile's tools need (unless ``offline``) plus its per-tool extras; each
    allows exactly the binaries of the tools using it."""
    names = {t.name for t in tools}
    problems = [f"network extras for {t!r}, which is not one of its tools"
                for t in prof.network if t not in names]
    users: dict[str, list[Tool]] = {}
    for t in tools:
        declared = [] if prof.offline else tool_entries[t.name].network
        for pol in dict.fromkeys([*declared, *prof.network.get(t.name, [])]):
            if pol in policies:
                users.setdefault(pol, []).append(t)
            else:
                problems.append(f"unknown network policy {pol!r} for tool {t.name} "
                                "(not in the library or proposed)")
    out = []
    for name, ts in users.items():
        pol = Policy(name=name, description=policies[name].description,
                     endpoints=tuple(policies[name].endpoints),
                     binaries=tuple(dict.fromkeys(b for t in ts for b in t.binaries)),
                     tools=tuple(t.name for t in ts))
        if not pol.binaries:
            problems.append(f"network policy {name} is for tools {list(pol.tools)}, which declare "
                            "no binaries; add them to the tool entries")
        out.append(pol)                  # still checked by the other guards (G-E2)
    return out, problems


def resolve(spec: EnvironmentSpec, lib: Library) -> tuple[dict[str, ResolvedProfile], list[str]]:
    """The spec's profiles resolved against the library and its proposals, and the problems
    (unknown names, shadowing, bad versions, unusable network); ``guards`` words them."""
    problems: list[str] = []
    for kind in Kind:
        for n in sorted(set(spec.proposed.entries(kind)) & set(lib.entries(kind))):
            problems.append(f"proposed {kind} {n!r} shadows the library entry of that name; "
                            "use the library entry or choose a new name")
    tools = lib.tools | spec.proposed.tools
    policies = lib.policies | spec.proposed.policies
    out: dict[str, ResolvedProfile] = {}
    for pname, prof in spec.profiles.items():
        rp = ResolvedProfile(name=pname, tools=[], policies=[], resources=prof.resources)
        mine: list[str] = []
        for ref in prof.tools:
            name, _, version = ref.partition("@")
            if name not in tools:
                mine.append(f"unknown tool {name!r} (not in the library or proposed)")
                continue
            t, err = resolve_tool(name, tools[name], version or None)
            if err:
                mine.append(err)
            elif t:
                rp.tools.append(t)
        rp.policies, nv = _network(prof, rp.tools, tools, policies)
        _, conflicts = merged_env(rp.tools)
        mine += nv + [f"conflicting tool environment: {c}" for c in conflicts]
        problems += [f"profile {pname}: {m}" for m in mine]
        out[pname] = rp
    problems += [f"role {role} mapped to unknown profile {p!r}"
                 for role, p in spec.roles.items() if p not in spec.profiles]
    return out, problems
