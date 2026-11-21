"""planner: spec, delta specs, oracle and task graph (G-P1..G-P11) and, with the environment
stage configured, the sandbox environment spec composed from the library (G-E1..G-E4)."""
from __future__ import annotations

from .. import governance, guards
from ..assets import GitMode
from ..engine.common import ENV_SPEC
from ..environment import EnvironmentSpec, Library, LibraryError
from ..model import Node
from ..view import RoleContext
from .base import Report, RoleHandler, fix_round_note, register, scaffold_note

NO_ENVIRONMENT = (f"No sandbox environment stage is configured: do not write `{ENV_SPEC}`; "
                  "every sandbox uses the default image.")


@register
class Planner(RoleHandler):
    roles = ("planner",)
    sees_oracle_commit = False

    def extra_context(self, e: RoleContext, node: Node) -> list[str]:
        """In a fix round: the round record and the integration failure (the approved plan
        documents are the common context)."""
        rnd = e.change.rounds[-1] if node.round > 1 and e.change.rounds else None
        return [a for a in ([rnd.attestation, *rnd.failure] if rnd else []) if a]

    def extra_variables(self, e: RoleContext, node: Node) -> dict[str, str]:
        """The environment part of the assignment: the library catalogue (an invalid library is
        reported, not raised: the spec is then rejected by G-E1 until an operator fixes it);
        in a fix round, what the round asks."""
        return self._environment_vars(e) | {"fix_round": fix_round_note(e, "planner"),
                                            "scaffold": scaffold_note(e, "planner")}

    def _environment_vars(self, e: RoleContext) -> dict[str, str]:
        if e.cfg.environment is None:
            return {"environment": NO_ENVIRONMENT}
        try:
            catalogue = Library.for_config(e.cfg, e.assets.schemas).catalogue()
        except LibraryError as x:
            catalogue = f"(the library is invalid; an operator must fix it: {x})"
        return {"environment": f"Also attest `{ENV_SPEC}` (required), following the sandbox "
                               "environment skill; on a rework, rewrite it only if the plan's "
                               f"needs changed. The library:\n\n{catalogue}"}

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        tg, e1 = guards.load_json(e.docs, r.docs[e.p("plan/taskgraph.json")], "G-P1")
        orc, e2 = guards.load_json(e.docs, r.docs[e.p("plan/oracle.json")], "G-P1")
        if e1 or e2:
            return e1 + e2
        souls = e.assets.roles["implementer"].souls_allowed
        if r.node.round > 1:          # a fix round appends to the task graph (G-FR1, G-FR2)
            built = {b.task.id: b.task.model_dump(mode="json") for b in e.build_nodes()
                     if b.round < r.node.round}           # earlier rounds' (this one's is redone)
            v = guards.fix_round(tg, orc, e.change.requirements, e.change.tier,
                                 e.cfg.protected_paths + governance.setup_paths(e), souls,
                                 e.docs.schemas, built)
        else:
            v = guards.plan(tg, orc, e.change.requirements, e.change.tier,
                            e.cfg.protected_paths + governance.setup_paths(e),
                            souls, e.docs.schemas, blank_repo=e.change.blank_repo)
        _, vs = guards.required_docs(r.run.id, [e.p(f"plan/specs/{q}.md") for q in e.change.requirements],
                                     r.parsed, e.docs, r.run.confirmed_docs)
        return v + [x.replace("G-R4", "G-P9") for x in vs] + self._environment(e, r)

    def _environment(self, e: RoleContext, r: Report) -> list[str]:
        """G-E1..G-E4 on the environment spec, when the stage is configured. The report attests
        its latest version, which a rework may leave as an earlier run wrote it."""
        if e.cfg.environment is None:
            return []
        path = e.p(ENV_SPEC)
        att = max((a for a in r.parsed if a.kind == "file" and a.path == path),
                  key=lambda a: a.version, default=None)
        if att is None:
            return [f"G-E1: {path} not attested (required with the environment stage)"]
        if att.version != e.latest(ENV_SPEC):
            return [f"G-E1: {att} is not the latest version ({e.latest(ENV_SPEC)})"]
        raw, err = guards.load_json(e.docs, att, "G-E1")
        if err:
            return err
        try:
            lib = Library.for_config(e.cfg, e.assets.schemas)
        except LibraryError as x:
            return [f"G-E1: the environment library is invalid (an operator must fix it): {x}"]
        _, v = guards.environment(EnvironmentSpec.model_validate(raw), lib, e.cfg, set(e.assets.roles),
                                  {n for n, r in e.assets.roles.items() if r.git != GitMode.NONE})
        return v

    async def on_done(self, e: RoleContext, r: Report) -> None:
        """The plan is critiqued next (the critic is dispatched while it is REVIEWING); it is
        accepted when the critic passes it."""
        e.step_done(r.node)                 # the critic next
