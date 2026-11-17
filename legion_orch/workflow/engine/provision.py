"""Provision: build the approved sandbox environment (G-PV1). Controller only, no agent.

The approved ``plan/environment.json`` (the version pinned by H-E1) is resolved against the
library again – the library may have changed since the spec was written – and everything a
mapped role needs is built: one image per (base, tool set), content-addressed so what several
roles or changes share is built once. Network policies carry no credentials, so nothing is
created for them in OpenShell: each becomes a ``network_policies`` entry of the role's sandbox
policy. The result is the change's :class:`EnvironmentBuild`, one :class:`RoleEnvironment`
per mapped role; dispatch and verification attach it (:func:`sandbox_env`). Any failure
escalates; retry provisions again.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ...sandbox import BuildError, ImageRecipe, SandboxEnv, driver_config, policy_entry
from .. import guards
from ..environment import VERIFIER, EnvironmentSpec, Library, LibraryError
from ..model import (
    ACCEPTED,
    H_ENV,
    PROVISION,
    RUNNING,
    Change,
    EnvironmentBuild,
    Resume,
    RoleEnvironment,
)
from .common import ENV_SPEC
from .core import ChangeCore
from .runs import RunLifecycle


@dataclass
class ProvisionPlan:
    """What the approved spec needs: per role its recipe, network policies and resources."""
    spec: str                                                    # attestation
    recipes: dict[str, ImageRecipe] = field(default_factory=dict)        # role -> image recipe
    roles: dict[str, RoleEnvironment] = field(default_factory=dict)      # role -> (image unset)


def sandbox_env(change: Change, role: str) -> SandboxEnv | None:
    """The provisioned environment of a sandbox serving ``role`` ("verifier" for verifiers)."""
    env = change.environment.roles.get(role) if change.environment else None
    if env is None:
        return None
    return SandboxEnv(image=env.image, network=dict(env.network), cpu=env.cpu,
                      memory=env.memory, driver_config=env.driver_config)


class Provisioner:
    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle):
        self.c, self.lifecycle = core, lifecycle

    def _fail(self, reason: str, log: str = "") -> None:
        if log:
            self.c.write_record("control/logs/provision.log", log)
        self.c.escalate(self.c.nodes[PROVISION], "provision_failed", f"G-PV1: {reason}"[:2000],
                        resume=Resume.READY, log_tail=log[-4000:] or None)

    def _plan(self) -> ProvisionPlan | None:
        """Under the lock: the approved spec resolved into what to build, or None (escalated)."""
        c = self.c
        appr = c.change.approvals.get(H_ENV)
        version = appr.pins.get(c.p(ENV_SPEC)) if appr else None
        if version is None:
            self._fail("no approved environment spec")
            return None
        spec = EnvironmentSpec.model_validate(c.json_doc(ENV_SPEC, version))
        try:
            lib = Library.for_config(c.cfg, c.assets.schemas)
        except LibraryError as e:
            self._fail(f"the environment library is invalid: {e}")
            return None
        profiles, v = guards.environment(spec, lib, c.cfg, set(c.assets.roles))
        if v:
            self._fail("the approved spec no longer resolves against the library: " + "; ".join(v))
            return None
        env, sc = c.cfg.environment, c.cfg.sandbox
        plan = ProvisionPlan(spec=f"file:{c.p(ENV_SPEC)}@{version}")
        for role, pname in spec.roles.items():
            prof = profiles[pname]
            base = (sc.verifier_image or sc.image) if role == VERIFIER else sc.image
            plan.recipes[role] = ImageRecipe.make(base or "", prof.tools, mise_version=env.mise_version,
                                                  user=sc.run_as_user, registry=env.registry)
            plan.roles[role] = RoleEnvironment(
                profile=pname, network=dict(policy_entry(p) for p in prof.policies),
                cpu=prof.resources.cpu, memory=prof.resources.memory,
                driver_config=driver_config(c.cfg, prof.resources))
        return plan

    async def provision(self) -> None:
        c = self.c
        async with c.lock:
            node = c.nodes.get(PROVISION)
            if node is None or node.status != RUNNING:
                return
            plan = self._plan()
            if plan is None:
                c.save()
                return
            recipes = {r.tag: r for r in plan.recipes.values()}
            c.event("provision", f"building {len(recipes)} image(s) for {plan.spec}", PROVISION)
        log: list[str] = []
        for role, env in sorted(plan.roles.items()):
            for key, entry in env.network.items():
                log.append(f"## {role}: network policy {key} (sandbox policy): "
                           f"binaries {[b['path'] for b in entry['binaries']]}")
        images: dict[str, str | None] = {}           # recipe tag -> image reference
        try:
            for tag, recipe in recipes.items():
                log.append(f"## image {tag}\n\n{recipe.files['Dockerfile']}")
                images[tag], out = await c.svc.builder.ensure_image(recipe)
                log.append(out)
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
            roles = {role: env.model_copy(update={"image": images[plan.recipes[role].tag]})
                     for role, env in plan.roles.items()}
            build = EnvironmentBuild(spec=plan.spec, roles=roles,
                                     log=c.write_record("control/logs/provision.log",
                                                        "\n\n".join(log)).attestation)
            build.attestation = c.write_record("control/environment/build.json", build).attestation
            c.change.environment = build
            # Sandboxes kept across cycles were created from the previous build (reprovision):
            # release them, so each node's next run starts fresh in this one.
            released = sorted({n.sandbox for n in c.nodes.values() if n.sandbox})
            for n in c.nodes.values():
                n.sandbox = None
            for sb in released:
                c.event("provision", f"released sandbox {sb} of the previous build", PROVISION)
                self.lifecycle.delete_sandbox_later(sb)
            node.status = ACCEPTED
            c.event("provision", f"environment ready for {', '.join(sorted(roles)) or 'no roles'}",
                    PROVISION)
            c.save()
