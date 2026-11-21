"""implementer: scoped commit + evidence, then deterministic verification (G-B3, G-B4, G-PP1)."""
from __future__ import annotations

from .. import governance, guards
from ..view import RoleContext
from .base import Report, RoleHandler, build_context, register


@register
class Implementer(RoleHandler):
    roles = ("implementer",)
    needs_fresh_approval = True
    sees_risk = False             # the risk signals set the tier; they don't shape the code
    task_scoped_specs = True

    def extra_context(self, e: RoleContext, node) -> list[str]:
        return build_context(e, node)

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        run, t = r.run, r.node.task
        changed = await e.git.changed_paths(run.base_sha, r.sha)
        approved = governance.approved_protected_for(e, t.id, run.created_at)  # G-PP1
        protected = [p for p in governance.protected_paths(e) if p not in approved]
        v = guards.scoped_diff(changed, t.files_modified + approved, t.files_forbidden,
                               protected, "G-B3")
        ev = e.docs.read(run.required_docs[0], r.docs[run.required_docs[0]].version).content
        return v + guards.evidence_mentions(ev, t.verification)

    async def on_done(self, e: RoleContext, r: Report) -> None:
        r.node.candidate_sha = r.sha
        await e.git.pin(r.run.id, r.sha)
        e.step_done(r.node)                 # verification next
