"""release (R3): observation and rollback plan (G-REL1)."""
from __future__ import annotations

from .. import guards
from ..view import RoleContext
from .base import Report, RoleHandler, evidence_of, register


@register
class Release(RoleHandler):
    roles = ("release",)

    def extra_context(self, e: RoleContext, node) -> list[str]:
        refs = [a for a in (evidence_of(e, b) for b in e.build_nodes()) if a]
        report = e.latest_att("integration/report.md")
        return refs + ([report] if report else [])

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        return guards.release_docs(e.doc("release/plan.md").content, e.doc("release/rollback.md").content,
                                   e.change.base_sha)
