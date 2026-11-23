"""Provision: build the approved sandbox environment (G-PV1). Controller only, no agent.

The approved environment spec (the ``environment`` pointer: H-E1 signed it) is resolved against the
library again – the library may have changed since the spec was written – and everything a
mapped role needs is built: one image per (base, tool set), content-addressed so what several
roles or changes share is built once. Network policies carry no credentials, so nothing is
created for them in OpenShell: each becomes a ``network_policies`` entry of the role's sandbox
policy. The result is the change's :class:`EnvironmentBuild`, one :class:`RoleEnvironment`
per mapped role; dispatch and verification attach it (:func:`sandbox_env`). A failure is the
provision ticket's (its executor: a fault a human fixes; a retry provisions again).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from ....sandbox import VERIFIER_PREFIX, BuildError, ImageRecipe, driver_config, policy_entry
from ... import guards
from ...assets import GitMode
from ...attest import FILE_RE
from ...environment import VERIFIER, EnvironmentSpec, Library, LibraryError
from ...model import LIVE_RUN, EnvironmentBuild, RoleEnvironment
from ...tickets import Ticket
from ...tickets.executors import Work as TicketWork
from ..base.core import ChangeCore
from ..base.dispatch import Dispatcher
from ..base.work import PREPARE, SETUP
from ..sandboxes.sandboxes import Sandboxes, sandbox_env  # noqa: F401  (sandbox_env: re-exported)
from .runs import RunLifecycle

PROVISION = "controller:provision"
PROVISION_LOG = "control/environment/provision.log"


@dataclass
class ProvisionPlan:
    """What the approved spec needs: per role its recipe, network policies and resources."""
    spec: str                                                    # attestation
    recipes: dict[str, ImageRecipe] = field(default_factory=dict)        # role -> image recipe
    roles: dict[str, RoleEnvironment] = field(default_factory=dict)      # role -> (image unset)


class Provisioner:
    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle, sandboxes: Sandboxes,
                 dispatcher: Dispatcher):
        self.c, self.lifecycle = core, lifecycle
        self.sandboxes, self.dispatcher = sandboxes, dispatcher

    def _fail(self, reason: str, log: str = "") -> str:
        """G-PV1: why provisioning stopped (the log is kept with the provision ticket's)."""
        if log:
            self.c.write_record(PROVISION_LOG, log)
        return f"G-PV1: {reason}"[:2000] + (f"\n\nlog tail:\n{log[-4000:]}" if log else "")

    def _plan(self) -> ProvisionPlan | str:
        """Under the lock: the approved spec resolved into what to build, or why it cannot be."""
        c = self.c
        ref = c.tickets.root.pointers.get("environment")
        m = FILE_RE.match(ref) if isinstance(ref, str) else None
        if m is None:
            return self._fail("no approved environment spec")
        path, version = m["path"], int(m["version"])
        spec = EnvironmentSpec.model_validate(json.loads(c.docs.read(path, version).content))
        try:
            lib = Library.for_config(c.cfg, c.assets.schemas)
        except LibraryError as e:
            return self._fail(f"the environment library is invalid: {e}")
        profiles, v = guards.environment(spec, lib, c.cfg, set(c.assets.roles),
                                         {n for n, r in c.assets.roles.items() if r.git != GitMode.NONE})
        if v:
            return self._fail("the approved spec no longer resolves against the library: "
                              + "; ".join(v))
        env, sc = c.cfg.environment, c.cfg.sandbox
        plan = ProvisionPlan(spec=f"file:{path}@{version}")
        for role, pname in spec.roles.items():
            prof = profiles[pname]
            base = (sc.verifier_image or sc.image) if role == VERIFIER else sc.image
            plan.recipes[role] = ImageRecipe.make(base or "", prof.tools, mise_version=env.mise_version,
                                                  user=sc.run_as_user, registry=env.registry,
                                                  change=c.cid)
            plan.roles[role] = RoleEnvironment(
                profile=pname, network=dict(policy_entry(p) for p in prof.policies),
                cpu=prof.resources.cpu, memory=prof.resources.memory,
                driver_config=driver_config(c.cfg, prof.resources))
        return plan

    async def provision(self, ticket: Ticket, tw: TicketWork, *,
                        wanted: Callable[[], bool], dispatch=None) -> str | None:
        """Build the approved environment for the provision ticket's round, then check the
        setup at its base (G-PV3): None when the environment is ready (or the round is no
        longer ``wanted``), else what failed. ``dispatch``: the setup verification's slot."""
        c = self.c
        async with c.lock:
            if not wanted():
                return None
            plan = self._plan()
            if isinstance(plan, str):
                return plan
            recipes = {r.tag: r for r in plan.recipes.values()}
            c.event("provision", f"building {len(recipes)} image(s) for {plan.spec}", ticket.id)
        log: list[str] = []
        for role, env in sorted(plan.roles.items()):
            for key, entry in env.network.items():
                log.append(f"## {role}: network policy {key} (sandbox policy): "
                           f"binaries {[b['path'] for b in entry['binaries']]}")
        images = await self.build_images(recipes, log)
        if isinstance(images, str):
            async with c.lock:
                if log:
                    c.write_record(PROVISION_LOG, "\n".join(log))
            return images
        async with c.lock:
            if not wanted():
                return None
            roles = {role: env.model_copy(update={"image": images[plan.recipes[role].tag]})
                     for role, env in plan.roles.items()}
            build = EnvironmentBuild(spec=plan.spec, roles=roles,
                                     log=c.write_record(PROVISION_LOG,
                                                        "\n\n".join(log)).attestation)
            c.write_record("control/environment/build.json", build)   # projected: change.environment
            # A kept sandbox of a role this build maps was created from an earlier build or the
            # default image: release it (after its agent reflects), so its ticket's next round
            # starts in this one. Other roles' sandboxes (the planner's) are untouched by builds.
            for t in c.tickets.t.values():
                sb = t.sandbox
                if not sb or not {r.role for r in c.runs.values() if r.sandbox == sb} & set(roles):
                    continue
                t.sandbox = None
                c.event("provision", f"released sandbox {sb}: not from this build", ticket.id)
                self.lifecycle.sandboxes.delete_later(sb)
            if c.change.setup_pin is None:
                c.event("provision", f"environment ready for {', '.join(sorted(roles)) or 'no roles'}",
                        ticket.id)
                return None
        return await self._check_setup(ticket, tw, sorted(roles), wanted, dispatch)

    async def _check_setup(self, ticket: Ticket, tw: TicketWork, roles: list[str], wanted,
                           dispatch=None) -> str | None:
        """G-PV3: the approved mise.toml's setup task succeeds at the ticket's base in a fresh
        verifier sandbox from this build – before any sandbox relies on it."""
        c = self.c
        work = self.sandboxes.verifier_work(c.host.new_sandbox_name(VERIFIER_PREFIX), "provision",
                                            tw.base or c.change.base_sha, (), in_new_build=True)
        outcome = await (dispatch or self.dispatcher).run(work)
        async with c.lock:
            if not wanted():
                return None
            stop = outcome.stopped
            infra = outcome.error or (f"{stop.command.label} failed ({stop.result.exit_code})"
                                      if stop and stop.command.kind == PREPARE else None)
            setup = outcome.results(SETUP)
            r = setup[0] if setup else None
            if infra or r is None or r.exit_code != 0:
                why = infra or (f"`{r.command}` exited {r.exit_code}" if r else "did not run")
                return self._fail(f"G-PV3: the repository's setup failed in the verifier: {why}",
                                  r.output if r else "")
            c.event("provision", "environment ready; the repository's setup ran in the "
                    f"verifier ({', '.join(roles) or 'no roles'})", ticket.id)
            return None

    async def build_images(self, recipes: dict[str, ImageRecipe],
                           log: list[str]) -> dict[str, str | None] | str:
        """Build (or find) the image of each recipe (G-PV1): recipe tag -> image reference, or
        what failed; ``log`` keeps what was built, for the provisioning record."""
        images: dict[str, str | None] = {}
        try:
            for tag, recipe in recipes.items():
                log.append(f"## image {tag}\n\n{recipe.files['Dockerfile']}")
                images[tag], out = await self.c.svc.builder.ensure_image(recipe)
                log.append(out)
        except (BuildError, OSError) as e:
            return self._fail(str(e), "\n".join(log))
        return images

    def environment_quiescent(self) -> bool:
        """Provisioning may start: no run of a role the current build maps is live and no
        verifier sandbox is in use (a verification waiting for the build uses none)."""
        c = self.c
        mapped = set(c.change.environment.roles) if c.change.environment else set()
        return not any(r.status in LIVE_RUN and r.role in mapped for r in c.runs.values()) \
            and not self.lifecycle.sandboxes.verifiers

    def waits_for_environment(self, role: str) -> bool:
        """A role the provisioned environment maps waits while it is being built again (a
        provision ticket is working), so no sandbox starts from the old build."""
        env = self.c.change.environment
        return bool(env and role in env.roles and any(
            t.type == PROVISION and t.status == "working" for t in self.c.tickets.t.values()))
