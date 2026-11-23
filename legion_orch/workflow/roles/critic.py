"""critic: a pre-mortem verdict on the plan it judges – its owner's outputs (G-C1, G-C2)."""
from __future__ import annotations

from .. import guards
from ..view import RoleContext
from .base import Report, RoleHandler, Said, judged, register


@register
class Critic(RoleHandler):
    roles = ("critic",)

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
