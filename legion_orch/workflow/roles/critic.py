"""critic: pre-mortem verdict on the plan and its sandbox environment (G-C1, G-C2). It judges
the plan node while it is REVIEWING; the node is accepted when it passes."""
from __future__ import annotations

from .. import governance, guards
from ..model import (
    Node,
    ReviewResult,
)
from ..view import RoleContext
from .base import (
    Report,
    RoleHandler,
    brief_findings,
    fix_round_note,
    record_review,
    register,
    scaffold_note,
)


@register
class Critic(RoleHandler):
    roles = ("critic",)
    judge = True
    # judges the plan node while it is REVIEWING, like a review-panel member a build task
    panel = True
    sees_draft_plan = True
    sees_oracle_commit = False

    def extra_context(self, e: RoleContext, node: Node) -> list[str]:
        att = e.latest_att(governance.env_spec(e, node.id)) if e.cfg.environment is not None else None
        rnd = e.change.rounds[-1] if node.round > 1 and e.change.rounds else None
        return [a for a in [att, *([rnd.attestation, *rnd.failure] if rnd else [])] if a]

    def extra_variables(self, e: RoleContext, node: Node) -> dict[str, str]:
        return {"fix_round": fix_round_note(e, "critic"), "scaffold": scaffold_note(e, "critic")}

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        plan_atts = {a.path: a.version for a in r.parsed if a.kind == "file"}
        d = r.node.id                                   # the plan it judges
        docs = governance.plan_docs(e, d) + (
            [governance.env_spec(e, d)] if e.cfg.environment is not None else [])
        latest = {e.p(p): e.latest(p) for p in docs}
        return guards.critique(r.data, plan_atts, latest, r.repo_atts)

    async def on_done(self, e: RoleContext, r: Report) -> None:
        """Records the critique; the plan step's critique phase says what it means (G-C2)."""
        run, data = r.run, r.data
        record_review(e, r, ReviewResult(
            verdict=data.verdict, run=run.id, findings=brief_findings(data.findings),
            doc=str(r.docs[run.required_docs[0]])))  # -> node.reviews
        return None
