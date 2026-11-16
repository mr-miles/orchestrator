"""critic: pre-mortem verdict on the plan and its sandbox environment (G-C1, G-C2)."""
from __future__ import annotations

from .. import guards
from ..engine.common import ENV_SPEC, PLAN_DOCS
from ..model import ACCEPTED, ENVIRONMENT, PENDING, PLAN, REWORK, Feedback, Node, Resume, Verdict
from ..view import RoleContext
from .base import Report, RoleHandler, register


@register
class Critic(RoleHandler):
    roles = ("critic",)
    judge = True
    sees_draft_plan = True
    sees_oracle_commit = False

    def extra_context(self, e: RoleContext, node: Node) -> list[str]:
        att = e.latest_att(ENV_SPEC) if ENVIRONMENT in e.nodes else None
        return [att] if att else []

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        plan_atts = {a.path: a.version for a in r.parsed if a.kind == "file"}
        docs = PLAN_DOCS + ([ENV_SPEC] if ENVIRONMENT in e.nodes else [])
        latest = {e.p(p): e.latest(p) for p in docs}
        return guards.critique(r.data, plan_atts, latest, r.repo_atts)

    async def on_done(self, e: RoleContext, r: Report) -> None:
        node, data = r.node, r.data
        e.accept_runs(node)
        if data.verdict == Verdict.PASS:
            node.status = ACCEPTED
            e.materialize_builds(e.json_doc("plan/taskgraph.json"))
            e.event("plan", f"plan accepted by critic; {len(e.build_nodes())} build task(s)")
            return
        plan = e.nodes[PLAN]
        limit = e.cfg.limits.critique_max_cycles + node.extra_cycles
        fb = Feedback(cycle=plan.cycle, source="critique", text="critique requested rework: "
                      + "; ".join(f"{f.severity}: {f.title}" for f in data.findings),
                      attestations=[str(a) for a in r.docs.values()])
        if plan.cycle >= limit:
            e.escalate(node, "critique_budget", f"G-C2: {plan.cycle} critique cycles without PASS",
                        resume=Resume.CRITIQUE_BUDGET)
            node.feedback.append(fb)
        else:
            plan.cycle += 1
            plan.feedback.append(fb)
            plan.status = REWORK
            e.reset_environment([fb])            # the environment follows the reworked plan
            node.cycle += 1
            node.status = PENDING
