"""implementer, scaffolder: a build's (or the plan's scaffold's) work – within its files (G-B3),
its evidence naming its verification (G-B4)."""
from __future__ import annotations

from ...git import PushScope
from .. import guards, scope
from ..model import Run
from ..view import RoleContext
from .base import Report, RoleHandler, output, register


@register
class Implementer(RoleHandler):
    roles = ("implementer", "scaffolder")

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        run, work = r.run, r.ticket.work
        changed = await e.git.changed_paths(run.base_sha, r.sha)
        v = scope.guard(e, e.tickets.t.get(r.ticket.id, r.ticket), changed)
        return v + guards.evidence_mentions(output(e, r, "evidence") or "",
                                            list(work.get("verification", [])))

    def push_scope(self, e: RoleContext, run: Run) -> PushScope:
        t = e.tickets.t.get(run.ticket)
        if t is None:                                   # no ticket: nothing is in its scope
            return PushScope("G-B3", run.base_sha)
        return scope.push_scope(e, t, run.base_sha)
