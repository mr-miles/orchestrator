"""Environment spec guards (G-E1..G-E4): the spec resolves against the library, its network
policies stay away from the controller and carry no credentials, resources fit the configured
caps, and only roles that run after provisioning are mapped."""
from __future__ import annotations

from ...config import Config
from ..environment import PRE_PROVISION_ROLES, VERIFIER, Library, ResolvedProfile, resolve

RESOURCE_CAPS = {"memory_mib": "max_memory_mib", "vcpus": "max_vcpus", "disk_mib": "max_disk_mib"}
LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
# Variable names that look like credentials (whole underscore segments, as OpenShell warns on).
SECRET_WORDS = {"TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "CREDENTIALS", "APIKEY"}
SECRET_PAIRS = {("API", "KEY"), ("ACCESS", "KEY"), ("PRIVATE", "KEY"), ("SECRET", "KEY")}


def looks_secret(name: str) -> bool:
    seg = name.split("_")
    return bool(SECRET_WORDS & set(seg)) or any(p in SECRET_PAIRS for p in zip(seg, seg[1:], strict=False))


def network(profiles: dict[str, ResolvedProfile], cfg: Config) -> list[str]:
    """G-E2: no endpoint on the controller's host or loopback, and no credential-like tool
    environment variable (network policies cannot carry credentials: the schema has no field)."""
    banned = {cfg.server.public_host, cfg.server.token_host, *LOOPBACK}
    v = []
    seen = set()
    for prof in profiles.values():
        for pol in prof.policies:
            for e in pol.endpoints:
                key = (pol.name, e["host"], e["port"])
                if key in seen:
                    continue
                seen.add(key)
                if e["host"].lower() in banned:
                    v.append(f"G-E2: network policy {pol.name}: {e['host']}:{e['port']} is the "
                             "controller host or loopback; controller access is granted by the "
                             "controller itself, never by an environment")
    for prof in profiles.values():
        for t in prof.tools:
            for k, _ in t.env:
                if looks_secret(k) and (t.name, k) not in seen:
                    seen.add((t.name, k))
                    v.append(f"G-E2: tool {t.name}: environment variable {k} looks like a "
                             "credential; the image environment is readable by every agent, so "
                             "credentials never go there")
    return v


def resources(profiles: dict[str, ResolvedProfile], cfg: Config) -> list[str]:
    """G-E3: requested resources within ``environment.max_*``."""
    env = cfg.environment
    v = []
    for prof in profiles.values():
        for key, cap_name in RESOURCE_CAPS.items():
            val, cap = prof.resources.get(key), getattr(env, cap_name)
            if val is not None and val > cap:
                v.append(f"G-E3: profile {prof.name}: {key} {val} exceeds environment.{cap_name} ({cap})")
    return v


def roles(spec: dict, known_roles: set[str]) -> list[str]:
    """G-E4: mapped roles exist and run after provisioning (or are the verifier)."""
    allowed = (known_roles - PRE_PROVISION_ROLES) | {VERIFIER}
    return [f"G-E4: role {r!r} cannot be given an environment (one of {sorted(allowed)})"
            for r in spec["roles"] if r not in allowed]


def environment(spec: dict, lib: Library, cfg: Config,
                known_roles: set[str]) -> tuple[dict[str, ResolvedProfile], list[str]]:
    """All spec guards; returns the resolved profiles and every violation."""
    profiles, v = resolve(spec, lib)
    return profiles, v + network(profiles, cfg) + resources(profiles, cfg) + roles(spec, known_roles)
