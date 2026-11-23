"""critic: a pre-mortem verdict on the plan it judges – its owner's outputs (G-C1, G-C2)."""
from __future__ import annotations

from .. import guards
from ..tickets import Ticket
from ..view import RoleContext
from .base import Report, RoleHandler, Said, judged, register


@register
class Critic(RoleHandler):
    roles = ("critic",)

    def extra_variables(self, e: RoleContext, ticket: Ticket) -> dict[str, str]:
        if not e.change.blank_repo:
            return {"scaffold": "The repository has code: a scaffold is needed only if its "
                                "layout or tooling must change first."}
        return {"scaffold": "**The repository is blank.** Check the scaffold's work: it sets "
                            "up only the project's layout and tooling, fixes every choice the "
                            "acceptance tests depend on (paths, runner, configuration), and its "
                            "verification passes without any test or feature the builds add."}

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        owner = e.tickets.t[r.ticket.parent] if r.ticket.parent else None
        attested = {a.path: a.version for a in r.parsed if a.kind == "file"}
        latest = {}
        for ref in (owner.outputs.values() if owner else []):
            for one in ref if isinstance(ref, list) else [ref]:
                path, _, version = one.removeprefix("file:").rpartition("@")
                latest[path] = int(version)
        return guards.critique(r.data, attested, latest, r.repo_atts)

    async def on_done(self, e: RoleContext, r: Report) -> Said:
        return judged(r.data.verdict, r.data.findings)
