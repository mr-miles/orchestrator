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

from ....git.branches import Branches, integration_branch
from ....sandbox import (
    VERIFIER_PREFIX,
    BuildError,
    ImageRecipe,
    Tool,
    driver_config,
    lookup_recipe,
    policy_entry,
)
from ....sandbox.images import merged_env
from ....util import now_iso
from ... import guards, setup
from ...assets import GitMode
from ...attest import FILE_RE
from ...environment import (
    LATEST,
    VERIFIER,
    EnvironmentSpec,
    Library,
    LibraryError,
    ToolVersion,
    all_tools,
    planned_versions,
    versions_toml,
    with_versions,
)
from ...model import LIVE_RUN, EnvironmentBuild, RoleEnvironment, ToolVersionRecord
from ...tickets import Ticket
from ...tickets.executors import Work as TicketWork
from ..base.core import ChangeCore
from ..base.dispatch import Dispatcher
from ..base.work import PREPARE, SETUP
from ..sandboxes.sandboxes import Sandboxes, sandbox_env  # noqa: F401  (sandbox_env: re-exported)
from .runs import RunLifecycle

PROVISION = "controller:provision"
PROVISION_LOG = "control/environment/provision.log"
MISE_DOC = "control/environment/mise.toml"      # the change's tool versions (every image's)


@dataclass
class ProvisionPlan:
    """What the approved spec needs: every tool (the same in every image) at the change's
    versions, and per role its base image, network policies and resources."""
    spec: str                                                    # attestation
    mise: str | None = None                       # the mise.toml it proposes (§5.3c), if any
    tools: list[Tool] = field(default_factory=list)              # every profile's, once
    versions: dict[str, ToolVersion] = field(default_factory=dict)   # mise tool -> its version
    bases: dict[str, str] = field(default_factory=dict)          # role -> the image built FROM
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

    async def _plan(self) -> ProvisionPlan | str:
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
        repo = None if spec.mise is not None or not c.change.base_sha else \
            await setup.read_at(c.git, c.change.base_sha)
        profiles, v = guards.environment(spec, lib, c.cfg, set(c.assets.roles),
                                         {n for n, r in c.assets.roles.items() if r.git != GitMode.NONE},
                                         repo_mise=repo)
        if v:
            return self._fail("the approved spec no longer resolves against the library: "
                              + "; ".join(v))
        sc = c.cfg.sandbox
        plan = ProvisionPlan(spec=f"file:{path}@{version}",
                             mise=spec.mise.content if spec.mise else None)
        # one tool set for every image, at one version each: the mise.toml that runs (the
        # proposed one, else the repository's), else this change's earlier versions, else latest
        plan.tools = all_tools(profiles)
        _, conflicts = merged_env(plan.tools)
        if conflicts:
            return self._fail("the profiles' tools set conflicting environment variables, and "
                              "every image has every tool: " + "; ".join(conflicts))
        running = setup.tools_at(plan.mise) if plan.mise is not None else \
            (c.change.base_tools or setup.tools_at(repo))
        kept = setup.tools_at(c.doc(MISE_DOC).content) if c.latest(MISE_DOC) else {}
        plan.versions = planned_versions(plan.tools, running, kept)
        for role, pname in spec.roles.items():
            prof = profiles[pname]
            plan.bases[role] = ((sc.verifier_image or sc.image) if role == VERIFIER else sc.image) or ""
            plan.roles[role] = RoleEnvironment(
                profile=pname, network=dict(policy_entry(p) for p in prof.policies),
                cpu=prof.resources.cpu, memory=prof.resources.memory,
                driver_config=driver_config(c.cfg, prof.resources))
        return plan

    async def provision(self, ticket: Ticket, tw: TicketWork, *,
                        wanted: Callable[[], bool]) -> str | None:
        """Build the approved environment for the provision ticket's round, then check the
        setup at its base (G-PV3): None when the environment is ready (or the round is no
        longer ``wanted``), else what failed."""
        c = self.c
        async with c.lock:
            if not wanted():
                return None
            plan = await self._plan()
            if isinstance(plan, str):
                return plan
            if plan.mise is not None:          # the approved mise.toml: committed, pinned (§5.3c)
                head = await Branches(c.git).commit_file(
                    integration_branch(c.cid), setup.MISE_TOML, plan.mise,
                    f"legion {c.cid}: the approved mise.toml")
                c.change.setup_pin = setup.pin_for(plan.mise, "H-E1")
                c.event("provision", f"committed the approved mise.toml at {head[:12]}",
                        ticket.id)
        log: list[str] = []
        versions = await self._versions(plan, log, ticket.id)
        if isinstance(versions, str):
            return versions
        env, sc = c.cfg.environment, c.cfg.sandbox
        tools = with_versions(plan.tools, versions)
        for role, base in plan.bases.items():
            plan.recipes[role] = ImageRecipe.make(base, tools, mise_version=env.mise_version,
                                                  user=sc.run_as_user, registry=env.registry,
                                                  change=c.cid)
        recipes = {r.tag: r for r in plan.recipes.values()}
        async with c.lock:
            mise_doc = c.write_record(MISE_DOC, versions_toml(versions)).attestation
            c.event("provision", f"building {len(recipes)} image(s) for {plan.spec}, every one "
                    f"with {', '.join(f'{k} {v}' for k, v in sorted(versions.items())) or 'no tools'}",
                    ticket.id)
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
            build = EnvironmentBuild(spec=plan.spec, roles=roles, mise=mise_doc, tools={
                k: ToolVersionRecord(version=versions[k], source=v.source,
                                     resolved_at=self._resolved_at if v.source == LATEST else None)
                for k, v in plan.versions.items()},
                log=c.write_record(PROVISION_LOG, "\n\n".join(log)).attestation)
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
        return await self._check_setup(ticket, tw, sorted(roles), wanted)

    async def _check_setup(self, ticket: Ticket, tw: TicketWork, roles: list[str],
                           wanted) -> str | None:
        """G-PV3: the approved mise.toml's setup task succeeds at the ticket's base in a fresh
        verifier sandbox from this build – before any sandbox relies on it."""
        c = self.c
        work = self.sandboxes.verifier_work(c.host.new_sandbox_name(VERIFIER_PREFIX), "provision",
                                            tw.base or c.change.base_sha, (), in_new_build=True)
        outcome = await self.dispatcher.run(work)
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

    async def _versions(self, plan: ProvisionPlan, log: list[str], tid: str
                        ) -> dict[str, str] | str:
        """Every tool's version: those still to decide looked up once, ``mise latest`` in the
        environment images' own mise (outside the lock: it reaches the network)."""
        out = {k: v.version for k, v in plan.versions.items() if v.version}
        todo = sorted(k for k, v in plan.versions.items() if not v.version)
        self._resolved_at = now_iso()
        if not todo:
            return out
        env = self.c.cfg.environment
        base = next(iter(plan.bases.values()), "") or self.c.cfg.sandbox.image or ""
        lookup = lookup_recipe(base, env.mise_version, self.c.cid, env.registry)
        try:
            found, text = await self.c.svc.builder.latest_versions(todo, lookup)
        except (BuildError, OSError) as e:
            return self._fail(f"looking up the latest versions of {', '.join(todo)}: {e}",
                              "\n".join(log))
        log.append(f"## latest versions of {', '.join(todo)}\n\n{text}")
        async with self.c.lock:
            self.c.event("provision", "latest: " + ", ".join(f"{k} {found[k]}" for k in todo), tid)
        return out | {k: found[k] for k in todo}

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
