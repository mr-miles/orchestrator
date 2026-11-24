"""planner: the spec, its delta specs, the oracle, the sandbox environment and the tickets – the
first replan, signed off at H-E1 (G-P1..G-P12 on the tickets' builds, G-E1..G-E4)."""
from __future__ import annotations

import json

from .. import guards, setup
from ..assets import GitMode
from ..environment import EnvironmentSpec, Library, LibraryError
from ..tickets import Ticket
from ..tickets.plan_doc import depends, parse_plan
from ..view import RoleContext
from .base import Report, RoleHandler, output, protected, register, requirement_ids

SCAFFOLD = "scaffold"            # the skeleton's scaffold ticket (workflow.yaml)
SCAFFOLD_BLANK = ("**The repository is blank.** Give `scaffold` its work (G-P11): the project's "
                  "layout, manifest, build, lint and test tooling and their configuration, and a "
                  "placeholder entry point – none of the requirements' behaviour – fixing every "
                  "choice the acceptance tests depend on; its verification passes without them.")
SCAFFOLD_CODE = ("The repository has code. Give `scaffold` its work only if the layout or "
                 "tooling must change before the tests and builds rely on it (a test runner, a "
                 "restructure); otherwise cancel it (G-P11).")

NO_ENVIRONMENT = ("No sandbox environment stage is configured: every sandbox uses the default "
                  "image. Your `environment` output is still required: write "
                  "`{\"summary\": \"the default image\", \"profiles\": {\"default\": {\"tools\": []}}, "
                  "\"roles\": {}, \"rationale\": \"no environment stage\"}`.")


@register
class Planner(RoleHandler):
    roles = ("planner",)

    def extra_variables(self, e: RoleContext, ticket: Ticket) -> dict[str, str]:
        """The environment part of the assignment: the library catalogue (an invalid library is
        reported, not raised: the spec is then rejected by G-E1 until an operator fixes it)."""
        scaffold = SCAFFOLD_BLANK if e.change.blank_repo else SCAFFOLD_CODE
        if e.cfg.environment is None:
            return {"environment": NO_ENVIRONMENT, "scaffold": scaffold}
        try:
            catalogue = Library.for_config(e.cfg, e.assets.schemas).catalogue()
        except LibraryError as x:
            catalogue = f"(the library is invalid; an operator must fix it: {x})"
        return {"environment": "Your `environment` output follows the sandbox environment "
                               "skill; on a rework, rewrite it only if the plan's needs changed. "
                               f"The library:\n\n{catalogue}", "scaffold": scaffold}

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        try:
            plan_data = json.loads(output(e, r, "tickets") or "")
            orc = json.loads(output(e, r, "oracle") or "")
        except ValueError as x:
            return [f"G-P1: an output is not JSON: {x}"]
        plan, v = parse_plan(plan_data)
        if v:
            return [f"G-P1: tickets: {x}" for x in v]
        # each ticket's work fits its role (the work schema – path shapes included), as its
        # sign-off checks again before applying it
        scaffold = plan.update.get(SCAFFOLD, {}).get("work")
        v = [f"G-P1: {x}" for a in plan.add for x in e.assets.work_problems(a.ticket)]
        if scaffold is not None:
            v += [f"G-P1: {x}" for x in e.assets.work_problems(
                Ticket(id=SCAFFOLD, type="agent:scaffolder", work=scaffold))]
        if v:
            return v
        after = depends(plan)
        builds = [a.ticket for a in plan.add if a.ticket.type == "agent:implementer"]
        tasks = [{"id": b.id, "title": b.goal or b.id, "depends_on": after.get(b.id, []), **b.work}
                 for b in builds]
        decision = "none" if (scaffold is not None) == (SCAFFOLD in plan.cancel) \
            else "work" if scaffold is not None else "cancel"
        sk = e.tickets.t.get(SCAFFOLD)               # decided already (a revised design): moot
        v = guards.scaffold(decision, e.change.blank_repo) \
            if sk is not None and sk.status == "pending" else []
        souls = e.assets.roles["implementer"].souls_allowed
        if tasks or scaffold:
            v += guards.plan({"tasks": tasks}, orc, requirement_ids(e), e.change.tier or "R0",
                             protected(e, orc), souls, e.docs.schemas, scaffold_work=scaffold)
        return v + await self._environment(e, r)

    async def _environment(self, e: RoleContext, r: Report) -> list[str]:
        """G-E1..G-E8 on the environment spec, when the stage is configured – G-E8 against the
        repository's mise.toml at the change's base when the spec proposes none."""
        if e.cfg.environment is None:
            return []
        try:
            raw = json.loads(output(e, r, "environment") or "")
        except ValueError as x:
            return [f"G-E1: environment is not JSON: {x}"]
        try:
            lib = Library.for_config(e.cfg, e.assets.schemas)
        except LibraryError as x:
            return [f"G-E1: the environment library is invalid (an operator must fix it): {x}"]
        spec = EnvironmentSpec.model_validate(raw)
        repo = None if spec.mise is not None or not e.change.base_sha else \
            await setup.read_at(e.git, e.change.base_sha)
        _, v = guards.environment(spec, lib, e.cfg, set(e.assets.roles),
                                  {n for n, r in e.assets.roles.items() if r.git != GitMode.NONE},
                                  repo_mise=repo)
        return v
