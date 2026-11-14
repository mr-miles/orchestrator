"""implementer: scoped commit + evidence, then deterministic verification (G-B3, G-B4, G-PP1)."""
from __future__ import annotations

from .. import governance, guards
from ..model import VERIFYING
from .base import Report, RoleHandler, register


@register
class Implementer(RoleHandler):
    roles = ("implementer",)

    async def guards(self, e, r: Report) -> list[str]:
        run, t = r.run, r.node.task
        changed = await e.git.changed_paths(run.base_sha, r.sha)
        approved = governance.approved_protected_for(e, t["id"], run.created_at)  # G-PP1
        protected = [p for p in governance.protected_paths(e) if p not in approved]
        v = guards.scoped_diff(changed, t["files_modified"] + approved, t.get("files_forbidden", []),
                               protected, "G-B3")
        ev = e.docs.read(run.required_docs[0], r.docs[run.required_docs[0]].version).content
        return v + guards.evidence_mentions(ev, t["verification"])

    async def on_done(self, e, r: Report) -> None:
        r.node.candidate_sha = r.sha
        await e.git.pin(r.run.id, r.sha)
        r.node.status = VERIFYING
        e._spawn(e._verify_node(r.node.id))
