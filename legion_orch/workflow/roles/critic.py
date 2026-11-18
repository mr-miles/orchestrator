"""critic: pre-mortem verdict on the plan and its sandbox environment (G-C1, G-C2). It judges
the plan node while it is REVIEWING; the node is accepted when it passes."""
from __future__ import annotations

from .. import guards
from ..engine.common import ENV_SPEC, PLAN_DOCS
from ..model import (
    ACCEPTED,
    H_ENV,
    R_ACCEPTED,
    R_REPORTED,
    R_SUPERSEDED,
    REWORK,
    Feedback,
    Node,
    Resume,
    ReviewResult,
    Verdict,
)
from ..view import RoleContext
from .base import Report, RoleHandler, brief_findings, fix_round_note, register


@register
class Critic(RoleHandler):
    roles = ("critic",)
    judge = True
    # judges the plan node while it is REVIEWING, like a review-panel member a build task
    panel = True
    sees_draft_plan = True
    sees_oracle_commit = False

    def extra_context(self, e: RoleContext, node: Node) -> list[str]:
        att = e.latest_att(ENV_SPEC) if H_ENV in e.nodes else None
        rnd = e.change.rounds[-1] if node.round > 1 and e.change.rounds else None
        return [a for a in [att, *([rnd.attestation, *rnd.failure] if rnd else [])] if a]

    def extra_variables(self, e: RoleContext, node: Node) -> dict[str, str]:
        return {"fix_round": fix_round_note(e, "critic")}

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        plan_atts = {a.path: a.version for a in r.parsed if a.kind == "file"}
        docs = PLAN_DOCS + ([ENV_SPEC] if H_ENV in e.nodes else [])
        latest = {e.p(p): e.latest(p) for p in docs}
        return guards.critique(r.data, plan_atts, latest, r.repo_atts)

    async def on_done(self, e: RoleContext, r: Report) -> None:
        """PASS accepts the plan and creates the build tasks; REWORK sends it back to the planner
        (a new plan cycle) within the critique budget (G-C2), else escalates."""
        run, node, data = r.run, r.node, r.data
        node.reviews[node.review_key(run.cycle, run.role)] = ReviewResult(
            verdict=data.verdict, run=run.id, findings=brief_findings(data.findings),
            doc=str(r.docs[run.required_docs[0]]))
        run.status = R_ACCEPTED
        if data.verdict == Verdict.PASS:
            node.status = ACCEPTED
            e.accept_runs(node)      # the critic's sandbox goes; the planner's is held to H-2
            if node.round > 1:       # a fix round: rerun what it changed, add its tasks
                ids = e.start_round(node.round)
                e.event("plan", f"fix round {node.round} accepted by critic; {len(ids)} new "
                        f"build task(s): {', '.join(ids)}")
                return
            e.materialize_builds(e.json_doc("plan/taskgraph.json"))
            e.event("plan", f"plan accepted by critic; {len(e.build_nodes())} build task(s)")
            return
        e.delete_sandbox_later(run.sandbox)
        planner = e.runs.get(node.current_run) if node.current_run else None
        if planner and planner.status == R_REPORTED:
            planner.status = R_SUPERSEDED
        node.feedback.append(Feedback(
            cycle=node.cycle, source="critique", text="critique requested rework: "
            + "; ".join(f"{f.severity}: {f.title}" for f in data.findings),
            attestations=[str(a) for a in r.docs.values()]))
        limit = e.cfg.limits.critique_max_cycles + node.extra_cycles
        cycles = node.cycle - node.round_start_cycle + 1      # a fix round has its own budget
        if cycles >= limit:
            e.escalate(node, "critique_budget", f"G-C2: {cycles} critique cycles without PASS",
                       resume=Resume.CRITIQUE_BUDGET)
        else:
            node.cycle += 1
            node.status = REWORK
