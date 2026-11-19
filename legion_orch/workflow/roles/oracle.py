"""oracle: protected acceptance tests, merged before any build (G-O1, G-O2)."""
from __future__ import annotations

from .. import guards
from ..model import H_SPEC, Oracle, Resume
from ..view import RoleContext
from .base import Report, RoleHandler, register, scaffold_note


@register
class OracleAuthor(RoleHandler):
    roles = ("oracle",)
    keeps_session_until = H_SPEC       # reworked (sent back) until this gate passes
    pushes = True
    sees_draft_plan = True
    sees_oracle_commit = False

    def extra_context(self, e: RoleContext, node) -> list[str]:
        """The scaffold's accepted commit: the oracle's tests run under its configuration."""
        s = e.scaffold_node()
        return [f"git:{e.cfg.repo.name}@{s.accepted_sha}"] if s and s.accepted_sha else []

    def extra_variables(self, e: RoleContext, node) -> dict[str, str]:
        return {"scaffold": scaffold_note(e, "oracle")}

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        orc = Oracle.model_validate(e.json_doc("plan/oracle.json"))
        changed = await e.git.changed_paths(r.run.base_sha, r.sha)
        allowed = orc.acceptance_tests + orc.protected_support
        v = guards.scoped_diff(changed, allowed, [], e.cfg.protected_paths, "G-O1")
        for p in orc.acceptance_tests:
            if not await e.git.has_object(r.sha, p):
                v.append(f"G-O2: acceptance test {p} missing at {r.sha[:12]}")
        return v

    async def on_done(self, e: RoleContext, r: Report) -> None:
        ch, node = e.change, r.node
        node.candidate_sha = r.sha
        await e.git.pin(r.run.id, r.sha)
        ok, head = await e.git.merge(ch.id, r.sha, f"legion {ch.id}: oracle (protected acceptance tests)")
        if not ok:
            e.escalate(node, "merge", f"oracle merge failed: {head[:400]}", resume=Resume.READY)
            return
        ch.oracle_sha, ch.integration_head = r.sha, head
        ch.oracle = Oracle.model_validate(e.json_doc("plan/oracle.json"))
        node.accepted_sha = r.sha
        # merged: the branch's commit is in the integration history, the evidence ref pins it
        if r.run.branch and await e.git.delete_ref(f"refs/heads/{r.run.branch}"):
            e.event("cleanup", f"deleted branch {r.run.branch} (merged)", node.id)
        await super().on_done(e, r)
