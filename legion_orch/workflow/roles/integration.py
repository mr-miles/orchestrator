"""integration: whole-change verdict at the integration head (G-INT1, G-INT2), then the
controller re-runs the oracle and surface commands deterministically."""
from __future__ import annotations

from .. import governance, guards
from ..model import BASELINE, Resume, ReviewResult, Verdict
from ..results import Escalate, Ok, PhaseResult
from ..view import RoleContext
from .base import Report, RoleHandler, brief_findings, evidence_of, record_review, register


@register
class Integration(RoleHandler):
    roles = ("integration",)
    judge = True

    def extra_context(self, e: RoleContext, node) -> list[str]:
        """Each task's evidence, (R3) the baseline – what failed before any build – and every
        fix round's record with the failure it set out to fix."""
        refs = [a for a in (evidence_of(e, b) for b in e.build_nodes()) if a]
        base = e.current(BASELINE)
        ver = base.verification.get("baseline") if base else None
        rounds = [a for rnd in e.change.rounds for a in (rnd.attestation, *rnd.failure) if a]
        report = f"{base.id}/report.md" if base else None
        return refs + [a for a in (e.latest_att(report) if report else None,
                                   ver.attestation if ver else None) if a] + rounds

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        v = guards.verdict(r.data, cycle=2, require_checked=False)
        v += guards.findings(r.data.findings, r.repo_atts, gid="G-INT1")
        v += guards.results_cover(r.data, e.oracle_commands(), "G-INT1")
        return v + await governance.protected_unchanged(e, r.sha)

    async def on_done(self, e: RoleContext, r: Report) -> PhaseResult:
        node, data = r.node, r.data
        record_review(e, r, ReviewResult(             # -> node.reviews
            verdict=data.verdict, sha=r.sha, run=r.run.id, findings=brief_findings(data.findings)))
        if data.verdict != Verdict.PASS:
            return Escalate("integration_failed", "integration verifier reported FAIL",
                            Resume.READY, run=r.run.id)
        node.candidate_sha = r.sha
        return Ok()                         # verification next
