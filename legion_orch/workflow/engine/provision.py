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

from ...sandbox import ImageRecipe, SandboxEnv, driver_config, policy_entry
from .. import guards
from ..assets import GitMode
from ..environment import VERIFIER, EnvironmentSpec, Library, LibraryError
from ..model import (
    ACCEPTED,
    H_ENV,
    LIVE_RUN,
    MERGING,
    RUNNING,
    VERIFYING,
    Change,
    EnvironmentBuild,
    Node,
    Resume,
    RoleEnvironment,
)
from ..model import PROVISION as PROVISION_KIND
from ..results import Escalate, Ok, PhaseResult
from .common import ENV_SPEC
from .core import ChangeCore
from .phases import BuildImages, PhaseContext, Verify
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
    # once the ended change was cleaned up its images are gone: a later run (a retrospective
    # asked for again) uses the default image; the record still says what was built
    return SandboxEnv(image=None if change.cleaned_at else env.image, network=dict(env.network),
                      cpu=env.cpu, memory=env.memory, driver_config=env.driver_config)


class Provisioner:
    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle, ctx: PhaseContext):
        self.c, self.lifecycle, self.ctx = core, lifecycle, ctx

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
        version = appr.pins.get(c.p(ENV_SPEC)) if appr else None
        if version is None:
            return self._fail(node, "no approved environment spec")
        spec = EnvironmentSpec.model_validate(c.json_doc(ENV_SPEC, version))
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
        plan = ProvisionPlan(spec=f"file:{c.p(ENV_SPEC)}@{version}")
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

    async def provision(self, node: Node, *, rebuild: bool = False) -> PhaseResult | None:
        """Build the approved environment for ``node`` (the plan's, or a fix round's), then check
        the setup (G-PV3). The result goes back to the BuildImages phase; None: the node was
        stopped meanwhile. ``rebuild``: reprovisioning an accepted environment (a change-level
        operation: the node is not run again)."""
        c = self.c

        def current() -> bool:
            return c.nodes.get(node.id) is node and (rebuild or node.status == RUNNING)
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
        built = await BuildImages(recipes, log).run(self.ctx)
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
            build.attestation = c.write_record("control/environment/build.json", build).attestation
            c.change.environment = build
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
        return await self._check_setup(node, sorted(roles), current)

    async def _check_setup(self, node: Node, roles: list[str], current) -> PhaseResult | None:
        """G-PV3: the approved mise.toml's setup task succeeds at the integration head in a fresh
        verifier sandbox from this build – before any sandbox relies on it."""
        c = self.c
        res = await Verify(()).verification(self.ctx, "provision", c.change.integration_head)
        async with c.lock:
            if not current():
                return None
            r = res.results[0] if res.results else None
            if res.infra_error or r is None or r.exit_code != 0:
                why = res.infra_error or (f"`{r.command}` exited {r.exit_code}" if r else "did not run")
                return self._fail(node, f"G-PV3: the repository's setup failed in the verifier: {why}",
                                  r.output if r else "")
            c.event("provision", "environment ready; the repository's setup ran in the "
                    f"verifier ({', '.join(roles) or 'no roles'})", node.id)
            return Ok()

    def environment_quiescent(self) -> bool:
        """Provisioning (or reprovisioning) may start: no run of a role the current build maps
        is live and nothing is verifying or merging in that build."""
        c = self.c
        mapped = set(c.change.environment.roles) if c.change.environment else set()
        return not any(r.status in LIVE_RUN and r.role in mapped for r in c.runs.values()) \
            and not any(n.status in (VERIFYING, MERGING) for n in c.nodes.values())

    def waits_for_environment(self, role: str) -> bool:
        """A role the provisioned environment maps waits while it is being built or rebuilt
        (``reprovision``), so no sandbox starts from the old build or the default image."""
        c = self.c
        prov, env = c.current(PROVISION_KIND), c.change.environment
        return bool(prov and env and role in env.roles
                    and (prov.status != ACCEPTED or c.change.reprovision))

    def start_requested_rebuild(self) -> None:
        """A requested reprovisioning starts once nothing uses the build."""
        c = self.c
        if c.change.reprovision == "requested" and self.environment_quiescent():
            c.change.reprovision = "running"
            c.spawn(self.rebuild())

    async def rebuild(self) -> None:
        """Reprovisioning (a change-level operation, requested by a decision owner): the
        accepted environment is built again from its approved spec against the current
        library. The provision node stays accepted; a failure is reported and the old build
        stays in place."""
        c = self.c
        node = c.current(PROVISION_KIND)
        result = await self.provision(node, rebuild=True) if node else None
        async with c.lock:
            c.change.reprovision = None
            if isinstance(result, Escalate):
                c.event("provision", f"reprovisioning failed, the earlier build stays: "
                        f"{result.question}", node.id if node else None)
            else:
                c.event("provision", "reprovisioned", node.id if node else None)
            c.save()
