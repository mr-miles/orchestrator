"""Provision: build the approved sandbox environment (G-PV1). Controller only, no agent.

The approved ``plan/environment.json`` (the version pinned by H-E1) is resolved against the
library again – the library may have changed since the spec was written – and every profile a
role uses is built: one image per (base, tool set) and one network provider per policy. Both
are content-addressed, so a profile shared by several roles or changes is built once. The
result is the change's :class:`EnvironmentBuild`; dispatch attaches it to each mapped role's
sandbox. Any failure escalates; retry provisions again.
"""
from __future__ import annotations

from ...sandbox import BuildError, ImageRecipe
from ...sandbox.environment import driver_config, image_recipe, network_profile
from .. import guards
from ..environment import VERIFIER, LibraryError
from ..model import (
    ACCEPTED,
    H_ENV,
    PROVISION,
    RUNNING,
    EnvironmentBuild,
    ProfileBuild,
    Resume,
)
from .common import ENV_SPEC
from .core import ChangeCore


class Provisioner:
    def __init__(self, core: ChangeCore):
        self.c = core

    def _fail(self, reason: str, log: str = "") -> None:
        node = self.c.nodes[PROVISION]
        if log:
            self.c.write_record("control/logs/provision.log", log)
        self.c.escalate(node, "provision_failed", f"G-PV1: {reason}"[:2000], resume=Resume.READY,
                        log_tail=log[-4000:] or None)

    def _plan(self) -> tuple[str, dict, dict[str, ImageRecipe], dict[str, list[dict]]] | None:
        """Under the lock: the approved spec resolved into what to build, or None (escalated)."""
        c = self.c
        appr = c.change.approvals.get(H_ENV)
        version = appr.pins.get(c.p(ENV_SPEC)) if appr else None
        if version is None:
            self._fail("no approved environment spec")
            return None
        spec = c.json_doc(ENV_SPEC, version)
        try:
            lib = c.environment_library()
        except LibraryError as e:
            self._fail(f"the environment library is invalid: {e}")
            return None
        profiles, v = guards.environment(spec, lib, c.cfg, set(c.assets.roles))
        if v:
            self._fail("the approved spec no longer resolves against the library: " + "; ".join(v))
            return None
        sc = c.cfg.sandbox
        recipes: dict[str, ImageRecipe] = {}       # "<profile>" / "<profile>:verifier" -> recipe
        networks: dict[str, list[dict]] = {}       # profile -> its provider profiles
        for role, pname in spec["roles"].items():
            prof = profiles[pname]
            if role == VERIFIER:
                key, base = f"{pname}:{VERIFIER}", sc.verifier_image or sc.image
            else:
                key, base = pname, sc.image
            if key not in recipes:
                recipes[key] = image_recipe(c.cfg, base or "", prof.tools)
            networks[pname] = [network_profile(pol) for pol in prof.policies]
        return f"file:{c.p(ENV_SPEC)}@{version}", spec, recipes, networks

    async def provision(self) -> None:
        c = self.c
        async with c.lock:
            node = c.nodes.get(PROVISION)
            if node is None or node.status != RUNNING:
                return
            planned = self._plan()
            if planned is None:
                c.save()
                return
            spec_att, spec, recipes, networks = planned
            unique = {p["id"]: p for ps in networks.values() for p in ps}
            c.event("provision", f"building {len(recipes)} image(s) and {len(unique)} network "
                    f"provider(s) for {spec_att}", PROVISION)
        log: list[str] = []
        images: dict[str, str | None] = {}
        try:
            for key, recipe in recipes.items():
                log.append(f"## image {key}: {recipe.tag}\n\n{recipe.files['Dockerfile']}")
                images[key], out = await c.svc.builder.ensure_image(recipe)
                log.append(out)
            for pid, prof in unique.items():
                log.append(f"## network provider {pid}: {prof['display_name']}, binaries "
                           f"{prof['binaries']}")
                log.append(await c.svc.builder.ensure_provider(prof))
        except (BuildError, OSError) as e:
            async with c.lock:
                if c.nodes[PROVISION].status == RUNNING:
                    self._fail(str(e), "\n".join(log))
                    c.save()
            return
        async with c.lock:
            node = c.nodes.get(PROVISION)
            if node is None or node.status != RUNNING:
                return
            builds: dict[str, ProfileBuild] = {}
            for pname, prof in spec["profiles"].items():
                if pname not in spec["roles"].values():
                    continue
                builds[pname] = ProfileBuild(
                    image=images.get(pname), verifier_image=images.get(f"{pname}:{VERIFIER}"),
                    providers=[p["id"] for p in networks.get(pname, [])],
                    driver_config=driver_config(c.cfg, prof.get("resources")))
            build = EnvironmentBuild(spec=spec_att, roles=dict(spec["roles"]), profiles=builds,
                                     log=c.write_record("control/logs/provision.log",
                                                        "\n\n".join(log)).attestation)
            build.attestation = c.write_record("control/environment/build.json", build).attestation
            c.change.environment = build
            node.status = ACCEPTED
            c.event("provision", f"environment ready: {', '.join(sorted(builds)) or 'no profiles'}",
                    PROVISION)
            c.save()

