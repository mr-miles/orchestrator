"""baseline (R3): independent results of the oracle commands before any build (G-BL1)."""
from __future__ import annotations

from .. import guards
from ..view import RoleContext
from .base import Report, RoleHandler, register


@register
class Baseline(RoleHandler):
    roles = ("baseline",)
    judge = True
    judges_head = True
    sees_draft_plan = True

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        orc = e.json_doc("plan/oracle.json")
        return guards.results_cover(r.data, orc.get("acceptance_commands", [])
                                    + orc.get("integration_commands", []), "G-BL1")
