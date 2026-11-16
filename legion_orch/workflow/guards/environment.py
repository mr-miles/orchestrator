"""Environment spec guards (G-E1..G-E4): the spec resolves against the library, its network
stays away from the controller, no tool puts a credential in the image environment, resources
fit the configured caps, and only roles that run after provisioning are mapped."""
from __future__ import annotations

from ...config import Config
from ...sandbox import Resources
from ..environment import (
    PRE_PROVISION_ROLES,
    VERIFIER,
    EnvironmentSpec,
    Library,
    ResolvedProfile,
    resolve,
)

LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
# Variable names that look like credentials (whole underscore segments, as OpenShell warns on).
SECRET_WORDS = {"TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "CREDENTIALS", "APIKEY"}
SECRET_PAIRS = {("API", "KEY"), ("ACCESS", "KEY"), ("PRIVATE", "KEY"), ("SECRET", "KEY")}


def looks_secret(name: str) -> bool:
    seg = name.split("_")
    return bool(SECRET_WORDS & set(seg)) or any(p in SECRET_PAIRS for p in zip(seg, seg[1:], strict=False))


def endpoints(profiles: dict[str, ResolvedProfile], cfg: Config) -> list[str]:
    """G-E2: no endpoint on the controller's host or loopback (network policies cannot carry
    credentials: the schema has no field for them)."""
    banned = {cfg.server.public_host, cfg.server.token_host, *LOOPBACK}
    bad = {(pol.name, e.host, e.port) for prof in profiles.values() for pol in prof.policies
           for e in pol.endpoints if e.host.lower() in banned}
    return [f"G-E2: network policy {n}: {h}:{p} is the controller host or loopback; controller "
            "access is granted by the controller itself, never by an environment"
            for n, h, p in sorted(bad)]


def secrets(profiles: dict[str, ResolvedProfile]) -> list[str]:
    """G-E2: every agent can read the image environment, so no credential goes there."""
    bad = {(t.name, k) for prof in profiles.values() for t in prof.tools for k, _ in t.env
           if looks_secret(k)}
    return [f"G-E2: tool {t}: environment variable {k} looks like a credential; the image "
            "environment is readable by every agent, so credentials never go there"
            for t, k in sorted(bad)]


def resources(profiles: dict[str, ResolvedProfile], cfg: Config) -> list[str]:
    """G-E3: requested resources within ``environment.max_*``."""
    env = cfg.environment
    caps = Resources(memory_mib=env.max_memory_mib, vcpus=env.max_vcpus, disk_mib=env.max_disk_mib)
    return [f"G-E3: profile {prof.name}: {k} {v} exceeds environment.max_{k} ({cap})"
            for prof in profiles.values() for k, v, cap in prof.resources.over(caps)]


def roles(spec: EnvironmentSpec, known_roles: set[str]) -> list[str]:
    """G-E4: mapped roles exist and run after provisioning (or are the verifier)."""
    allowed = (known_roles - PRE_PROVISION_ROLES) | {VERIFIER}
    return [f"G-E4: role {r!r} cannot be given an environment (one of {sorted(allowed)})"
            for r in spec.roles if r not in allowed]


def environment(spec: EnvironmentSpec, lib: Library, cfg: Config,
                known_roles: set[str]) -> tuple[dict[str, ResolvedProfile], list[str]]:
    """All spec guards; returns the resolved profiles and every violation."""
    profiles, problems = resolve(spec, lib)
    return profiles, ([f"G-E1: {p}" for p in problems] + endpoints(profiles, cfg)
                      + secrets(profiles) + resources(profiles, cfg) + roles(spec, known_roles))
