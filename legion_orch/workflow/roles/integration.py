"""integration: whole-change verdict at the integration head (G-INT1, G-INT2), then the
controller re-runs the oracle and surface commands deterministically."""
from __future__ import annotations

from .. import guards
from ..model import VERIFYING, Resume, ReviewResult
from ..view import RoleContext
from .base import Report, RoleHandler, brief_findings, evidence_of, register


@register
class Integration(RoleHandler):
    roles = ("integration",)
    judge = True
    judges_head = True

    def extra_context(self, e: RoleContext, node) -> list[str]:
        return [a for a in (evidence_of(e, b) for b in e.build_nodes()) if a]

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        v = guards.verdict(r.data, pass_word="PASS", fail_word="FAIL", gid="G-INT1", cycle=2,
                           require_checked=False)
        v += [x.replace("G-RV2", "G-INT1") for x in guards.findings(r.data, r.repo_atts)]
        v += guards.results_cover(r.data, e.oracle_commands(), "G-INT1")
        return v + await e.protected_unchanged(r.sha)

    async def on_done(self, e: RoleContext, r: Report) -> None:
        node, data = r.node, r.data
        node.reviews["integration"] = ReviewResult(
            verdict=data["verdict"], sha=r.sha, run=r.run.id,
            findings=brief_findings(data.get("findings", [])))
        if data["verdict"] != "PASS":
            e.escalate(node, "integration_failed", "integration verifier reported FAIL",
                        resume=Resume.READY, run=r.run.id)
            return
        node.candidate_sha = r.sha
        node.status = VERIFYING
        e.start_verification(node.id)
