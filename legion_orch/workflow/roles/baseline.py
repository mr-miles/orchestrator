"""baseline (R3): independent results of the oracle commands before any build (G-BL1); the
controller runs them too, and H-2 shows what already passes or disagrees (G-BL2)."""
from __future__ import annotations

from .. import guards
from ..model import VERIFYING, Oracle
from ..view import RoleContext
from .base import Report, RoleHandler, register


@register
class Baseline(RoleHandler):
    roles = ("baseline",)
    judge = True
    judges_head = True
    sees_draft_plan = True

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        orc = Oracle.model_validate(e.json_doc("plan/oracle.json"))
        return guards.results_cover(r.data, orc.acceptance_commands
                                    + orc.integration_commands, "G-BL1")

    async def on_done(self, e: RoleContext, r: Report) -> None:
        """The controller runs the same commands at the head itself (G-BL2 compares them)."""
        r.node.candidate_sha = r.sha
        r.node.status = VERIFYING
        e.start_verification(r.node.id)
