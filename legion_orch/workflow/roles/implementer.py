"""implementer, scaffolder: a build's (or the plan's scaffold's) work – within its files (G-B3),
its evidence naming its verification (G-B4)."""
from __future__ import annotations

from .. import guards
from ..view import RoleContext
from .base import Report, RoleHandler, guarded, oracle_paths, output, register


@register
class Implementer(RoleHandler):
    roles = ("implementer", "scaffolder")

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        run, work = r.run, r.ticket.work
        changed = await e.git.changed_paths(run.base_sha, r.sha)
        v = guards.scoped_diff(changed, list(work.get("files_modified", [])),
                               list(work.get("files_forbidden", [])), oracle_paths(e), "G-B3",
                               reasons=r.data.reasons(), guarded=guarded(e),
                               why_protected="one of the oracle's tests, which only the oracle "
                               "changes – revert it, and ask oracle@oracle if you think the test "
                               "is wrong")
        return v + guards.evidence_mentions(output(e, r, "evidence") or "",
                                            list(work.get("verification", [])))
