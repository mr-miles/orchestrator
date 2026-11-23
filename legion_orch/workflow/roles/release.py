"""release: the release plan and rollback (G-RL1)."""
from __future__ import annotations

from .. import guards
from ..view import RoleContext
from .base import Report, RoleHandler, output, outputs_named, register


@register
class Release(RoleHandler):
    roles = ("release",)

    def extra_context(self, e: RoleContext, ticket) -> list[str]:
        return outputs_named(e, "evidence") + outputs_named(e, "report", "agent:integration")

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        return guards.release_docs(output(e, r, "plan") or "", output(e, r, "rollback") or "",
                                   e.change.base_sha)
