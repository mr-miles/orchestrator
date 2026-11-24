"""reviewer, security, architecture: a judge of a build ticket's commit (G-RV*)."""
from __future__ import annotations

from .. import guards
from ..view import RoleContext
from .base import Report, RoleHandler, Said, judged, outputs_of_owner, register


@register
class Panel(RoleHandler):
    roles = ("reviewer", "security", "architecture")

    def extra_context(self, e: RoleContext, ticket) -> list[str]:
        """The judged work's evidence (its owner's outputs)."""
        return outputs_of_owner(e, ticket)

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        return guards.findings(r.data.findings, r.repo_atts) + guards.verdict(
            r.data, cycle=r.run.attempt, require_checked=True)

    async def on_done(self, e: RoleContext, r: Report) -> Said:
        return judged(r.data.verdict, r.data.findings)
