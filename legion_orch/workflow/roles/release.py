"""release (R3): observation and rollback plan (G-REL1)."""
from __future__ import annotations

from .. import guards
from .base import Report, RoleHandler, register


@register
class Release(RoleHandler):
    roles = ("release",)

    async def guards(self, e, r: Report) -> list[str]:
        return guards.release_docs(e.doc("release/plan.md").content, e.doc("release/rollback.md").content,
                                   e.change.base_sha)
