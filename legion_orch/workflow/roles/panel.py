"""Review panel (reviewer, security, architecture): verdict and findings on the candidate
(G-RV1..G-RV4); the panel outcome is decided once every member has reported (G-RV5)."""
from __future__ import annotations

from .. import guards
from ..model import ReviewResult
from ..view import RoleContext
from .base import Report, RoleHandler, brief_findings, build_context, evidence_of, record_review, register


@register
class Panel(RoleHandler):
    roles = ("reviewer", "security", "architecture")
    judge = True
    panel = True
    task_scoped_specs = True

    def extra_context(self, e: RoleContext, node) -> list[str]:
        refs = build_context(e, node)
        ev = evidence_of(e, node)
        ver = node.current_verification()
        return refs + [a for a in (ev, ver and ver.attestation) if a]

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        return guards.findings(r.data.findings, r.repo_atts) + guards.verdict(
            r.data, cycle=r.run.cycle, require_checked=True)

    async def on_done(self, e: RoleContext, r: Report) -> None:
        """Records the review; the panel decides once every judge has reported."""
        run, data = r.run, r.data
        record_review(e, r, ReviewResult(
            verdict=data.verdict, sha=run.judged_sha, run=run.id,
            findings=brief_findings(data.findings), checked=data.checked,
            doc=str(r.docs[run.required_docs[0]])))  # -> node.reviews
        return None
