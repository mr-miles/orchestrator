"""planner: the spec, its delta specs, the oracle, the sandbox environment and the tickets – the
first replan, signed off at H-E1 (G-P1..G-P12 on the tickets' builds, G-E1..G-E4)."""
from __future__ import annotations

import json

from .. import guards
from ..assets import GitMode
from ..environment import EnvironmentSpec, Library, LibraryError
from ..tickets import Ticket
from ..tickets.plan_doc import depends, parse_plan
from ..view import RoleContext
from .base import Report, RoleHandler, output, protected, register, requirement_ids

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
        if e.cfg.environment is None:
            return {"environment": NO_ENVIRONMENT}
        try:
            catalogue = Library.for_config(e.cfg, e.assets.schemas).catalogue()
        except LibraryError as x:
            catalogue = f"(the library is invalid; an operator must fix it: {x})"
        return {"environment": "Your `environment` output follows the sandbox environment "
                               "skill; on a rework, rewrite it only if the plan's needs changed. "
                               f"The library:\n\n{catalogue}"}

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        try:
            plan_data = json.loads(output(e, r, "tickets") or "")
            orc = json.loads(output(e, r, "oracle") or "")
        except ValueError as x:
            return [f"G-P1: an output is not JSON: {x}"]
        plan, v = parse_plan(plan_data)
        if v:
            return [f"G-P1: tickets: {x}" for x in v]
        after = depends(plan)
        builds = [a.ticket for a in plan.add if a.ticket.type == "agent:implementer"]
        tasks = [{"id": b.id, "title": b.goal or b.id, "depends_on": after.get(b.id, []), **b.work}
                 for b in builds]
        souls = e.assets.roles["implementer"].souls_allowed
        v = guards.plan({"tasks": tasks}, orc, requirement_ids(e), e.change.tier or "R0",
                        protected(e, orc), souls, e.docs.schemas,
                        blank_repo=e.change.blank_repo) if tasks else []
        return v + self._environment(e, r)

    def _environment(self, e: RoleContext, r: Report) -> list[str]:
        """G-E1..G-E4 on the environment spec, when the stage is configured."""
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
        _, v = guards.environment(EnvironmentSpec.model_validate(raw), lib, e.cfg, set(e.assets.roles),
                                  {n for n, r in e.assets.roles.items() if r.git != GitMode.NONE})
        return v
