"""oracle: protected acceptance tests, merged before any build (G-O1, G-O2)."""
from __future__ import annotations

from .. import guards
from ..model import READY
from ..view import RoleContext
from .base import Report, RoleHandler, register


@register
class Oracle(RoleHandler):
    roles = ("oracle",)
    pushes = True
    sees_draft_plan = True
    sees_oracle_commit = False

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        orc = e.json_doc("plan/oracle.json")
        changed = await e.git.changed_paths(r.run.base_sha, r.sha)
        allowed = orc.get("acceptance_tests", []) + orc.get("protected_support", [])
        v = guards.scoped_diff(changed, allowed, [], e.cfg.protected_paths, "G-O1")
        for p in orc.get("acceptance_tests", []):
            if not await e.git.has_object(r.sha, p):
                v.append(f"G-O2: acceptance test {p} missing at {r.sha[:12]}")
        return v

    async def on_done(self, e: RoleContext, r: Report) -> None:
        ch, node = e.change, r.node
        node.candidate_sha = r.sha
        await e.git.pin(r.run.id, r.sha)
        ok, head = await e.git.merge(ch.id, r.sha, f"legion {ch.id}: oracle (protected acceptance tests)")
        if not ok:
            e.escalate(node, "merge", f"oracle merge failed: {head[:400]}", resume=READY)
            return
        ch.oracle_sha, ch.integration_head = r.sha, head
        ch.oracle = e.json_doc("plan/oracle.json")
        node.accepted_sha = r.sha
        await super().on_done(e, r)
