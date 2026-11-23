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

from ....sandbox import VERIFIER_PREFIX, BuildError, ImageRecipe, driver_config, policy_entry
from ... import governance, guards
from ...assets import GitMode
from ...environment import VERIFIER, EnvironmentSpec, Library, LibraryError
from ...model import (
    ACCEPTED,
    H_ENV,
    LIVE_RUN,
    RUNNING,
    EnvironmentBuild,
    Node,
    Resume,
    RoleEnvironment,
)
from ...model import PROVISION as PROVISION_KIND
from ...results import Escalate, Ok, PhaseResult
from ..base.core import ChangeCore
from ..base.dispatch import Dispatcher
from ..base.work import PREPARE, SETUP
from ..sandboxes.sandboxes import Sandboxes, sandbox_env  # noqa: F401  (sandbox_env: re-exported)
from .runs import RunLifecycle


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

    def _fail(self, node: Node, reason: str, log: str = "") -> Escalate:
        """G-PV1: the result that stops provisioning (the log is kept)."""
        if log:
            self.c.write_record("control/logs/provision.log", log)
        return Escalate("provision_failed", f"G-PV1: {reason}"[:2000], Resume.READY,
                        log_tail=log[-4000:] or None)

    def _plan(self, node: Node) -> ProvisionPlan | Escalate:
        """Under the lock: the approved spec resolved into what to build, or why it cannot be."""
        c = self.c
        appr = c.change.approvals.get(H_ENV)
        path, version = governance.pinned(appr, governance.ENV_FILE)
        if version is None:
            return self._fail(node, "no approved environment spec")
        spec = EnvironmentSpec.model_validate(c.json_doc(path, version))
        try:
            lib = Library.for_config(c.cfg, c.assets.schemas)
        except LibraryError as e:
            return self._fail(node, f"the environment library is invalid: {e}")
        profiles, v = guards.environment(spec, lib, c.cfg, set(c.assets.roles),
                                         {n for n, r in c.assets.roles.items() if r.git != GitMode.NONE})
        if v:
            return self._fail(node, "the approved spec no longer resolves against the library: "
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

    async def provision(self, node: Node, *, dispatch=None) -> PhaseResult | None:
        """Build the approved environment for ``node`` (the plan's, a fix round's, or the same
        again – an escalation answered by running the step again), then check the setup
        (G-PV3). The result goes back to the BuildImages phase; None: the node was stopped
        meanwhile. ``dispatch``: the step's dispatcher, for the setup's verification."""
        c = self.c

        def current() -> bool:
            return c.nodes.get(node.id) is node and node.status == RUNNING
        async with c.lock:
            if not current():
                return None
            plan = self._plan(node)
            if isinstance(plan, Escalate):
                return plan
            recipes = {r.tag: r for r in plan.recipes.values()}
            c.event("provision", f"building {len(recipes)} image(s) for {plan.spec}", node.id)
        log: list[str] = []
        for role, env in sorted(plan.roles.items()):
            for key, entry in env.network.items():
                log.append(f"## {role}: network policy {key} (sandbox policy): "
                           f"binaries {[b['path'] for b in entry['binaries']]}")
        built = await self.build_images(recipes, log)
        if not isinstance(built, Ok):
            async with c.lock:
                if log:
                    c.write_record("control/logs/provision.log", "\n".join(log))
            return built
        images: dict[str, str | None] = built.value          # recipe tag -> image reference
        async with c.lock:
            if not current():
                return None
            roles = {role: env.model_copy(update={"image": images[plan.recipes[role].tag]})
                     for role, env in plan.roles.items()}
            build = EnvironmentBuild(spec=plan.spec, roles=roles,
                                     log=c.write_record("control/logs/provision.log",
                                                        "\n\n".join(log)).attestation)
            c.write_record("control/environment/build.json", build)   # projected: change.environment
            # A kept sandbox of a role this build maps was created from an earlier build or the
            # default image: release it (after its agent reflects), so the node's next run
            # starts in this one. Other roles' sandboxes (the planner's) are untouched by builds.
            for sb in sorted({n.sandbox for n in c.nodes.values() if n.sandbox}):
                if not {r.role for r in c.runs.values() if r.sandbox == sb} & set(roles):
                    continue
                stopped = False
                for n in c.nodes.values():
                    if n.sandbox == sb:
                        stopped = stopped or n.sandbox_stopped
                        n.sandbox, n.sandbox_stopped = None, False
                c.event("provision", f"released sandbox {sb}: not from this build", node.id)
                self.lifecycle.sandboxes.delete_later(sb, stopped)
            pin = c.change.setup_pin
            check = pin is not None and pin.source == H_ENV
            if check and c.setup_waits_for_scaffold():
                check = False
                c.event("provision", "the repository is blank: its setup is checked at the "
                        "scaffold's verification (what it needs comes with the scaffold)", node.id)
            if not check:
                c.event("provision", f"environment ready for {', '.join(sorted(roles)) or 'no roles'}",
                        node.id)
                return Ok()
        return await self._check_setup(node, sorted(roles), current, dispatch)

    async def _check_setup(self, node: Node, roles: list[str], current,
                           dispatch=None) -> PhaseResult | None:
        """G-PV3: the approved mise.toml's setup task succeeds at the integration head in a fresh
        verifier sandbox from this build – before any sandbox relies on it."""
        c = self.c
        work = self.sandboxes.verifier_work(c.host.new_sandbox_name(VERIFIER_PREFIX), "provision",
                                            c.change.integration_head, (), in_new_build=True)
        outcome = await (dispatch or self.dispatcher).run(work)
        async with c.lock:
            if not current():
                return None
            stop = outcome.stopped
            infra = outcome.error or (f"{stop.command.label} failed ({stop.result.exit_code})"
                                      if stop and stop.command.kind == PREPARE else None)
            setup = outcome.results(SETUP)
            r = setup[0] if setup else None
            if infra or r is None or r.exit_code != 0:
                why = infra or (f"`{r.command}` exited {r.exit_code}" if r else "did not run")
                return self._fail(node, f"G-PV3: the repository's setup failed in the verifier: {why}",
                                  r.output if r else "")
            c.event("provision", "environment ready; the repository's setup ran in the "
                    f"verifier ({', '.join(roles) or 'no roles'})", node.id)
            return Ok()

    async def build_images(self, recipes: dict[str, ImageRecipe],
                           log: list[str]) -> Ok | Escalate:
        """Build (or find) the image of each recipe (G-PV1). Ok: recipe tag -> image reference;
        ``log`` keeps what was built, for the provisioning record."""
        images: dict[str, str | None] = {}
        try:
            for tag, recipe in recipes.items():
                log.append(f"## image {tag}\n\n{recipe.files['Dockerfile']}")
                images[tag], out = await self.c.svc.builder.ensure_image(recipe)
                log.append(out)
        except (BuildError, OSError) as e:
            return Escalate("provision_failed", f"G-PV1: {e}"[:2000], Resume.READY,
                            log_tail="\n".join(log)[-4000:] or None)
        return Ok(images)

    def environment_quiescent(self) -> bool:
        """Provisioning may start: no run of a role the current build maps is live and no
        verifier sandbox is in use (a verification waiting for the build uses none)."""
        c = self.c
        mapped = set(c.change.environment.roles) if c.change.environment else set()
        return not any(r.status in LIVE_RUN and r.role in mapped for r in c.runs.values()) \
            and not self.lifecycle.sandboxes.verifiers

    def waits_for_environment(self, role: str) -> bool:
        """A role the provisioned environment maps waits while it is being built again (its
        provision step running again, or a fix round's not done), so no sandbox starts from the
        old build."""
        c = self.c
        prov, env = c.current(PROVISION_KIND), c.change.environment
        return bool(prov and env and role in env.roles and prov.status != ACCEPTED)
