"""describer and pr-reviewer: the change's pull request description, written at the integration
head (G-PR1) and judged faithful to the change's motivation and to what changed (G-PR2); the
describe step's critique phase decides what the review means (G-PR3)."""
from __future__ import annotations

from .. import governance, guards
from ..model import INTEGRATION, Node, ReviewResult, Verdict
from ..results import Ok, PhaseResult
from ..view import RoleContext
from .base import Report, RoleHandler, brief_findings, evidence_of, record_review, register


def _change_context(e: RoleContext) -> list[str]:
    """What a description must be faithful to: the goal, the plan, each task's evidence, the
    integration report and every fix round's record."""
    docs = [e.latest_att(p) for p in ("control/goal.md", e.change.plan_doc("spec.md"),
                                      e.change.plan_doc("taskgraph.json"),
                                      governance.step_doc(e, INTEGRATION, "report.md"))]
    evidence = [evidence_of(e, b) for b in e.build_nodes()]
    rounds = [a for rnd in e.change.rounds for a in (rnd.attestation, *rnd.failure)]
    return [a for a in (*docs, *evidence, *rounds) if a]


@register
class Describer(RoleHandler):
    roles = ("describer",)

    def extra_context(self, e: RoleContext, node: Node) -> list[str]:
        return _change_context(e)

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        return guards.pr_description(e.doc(guards.PR_DOC).content, list(e.change.requirements))

    async def on_done(self, e: RoleContext, r: Report) -> PhaseResult:
        """What the PR reviewer judges next: the description, at the head it describes."""
        r.node.candidate_sha = r.sha
        return Ok()


@register
class PrReviewer(RoleHandler):
    roles = ("pr-reviewer",)
    judge = True
    panel = True                      # judges the describe node while it is REVIEWING

    def extra_context(self, e: RoleContext, node: Node) -> list[str]:
        pr = e.latest_att(guards.PR_DOC)
        return _change_context(e) + ([pr] if pr else [])

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        attested = {a.path: a.version for a in r.parsed if a.kind == "file"}
        v = guards.pr_review(attested, e.p(guards.PR_DOC), e.latest(guards.PR_DOC))
        if r.data.verdict == Verdict.REWORK and not r.data.findings:
            v.append("G-PR2: REWORK requires at least one finding")
        return v + guards.findings(r.data.findings, r.repo_atts, gid="G-PR2")

    async def on_done(self, e: RoleContext, r: Report) -> None:
        """Records the review; the describe step's critique phase says what it means."""
        run, data = r.run, r.data
        record_review(e, r, ReviewResult(
            verdict=data.verdict, sha=run.judged_sha, run=run.id,
            findings=brief_findings(data.findings), doc=str(r.docs[run.required_docs[0]])))  # -> node.reviews
        return None

