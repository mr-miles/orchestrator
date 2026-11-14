"""critic: pre-mortem verdict on the plan (G-C1, G-C2)."""
from __future__ import annotations

from .. import guards
from ..engine.common import PLAN_DOCS
from ..model import ACCEPTED, PENDING, PLAN, REWORK
from .base import Report, RoleHandler, register


@register
class Critic(RoleHandler):
    roles = ("critic",)

    async def guards(self, e, r: Report) -> list[str]:
        plan_atts = {a.path: a.version for a in r.parsed if a.kind == "file"}
        latest = {e.p(p): e.latest(p) for p in PLAN_DOCS}
        return guards.critique(r.data, plan_atts, latest, r.repo_atts)

    async def on_done(self, e, r: Report) -> None:
        node, data = r.node, r.data
        e._accept_runs(node)
        if data["verdict"] == "PASS":
            node.status = ACCEPTED
            e._materialize_builds(e.json_doc("plan/taskgraph.json"))
            e._event("plan", f"plan accepted by critic; {len(e.build_nodes())} build task(s)")
            return
        plan = e.nodes[PLAN]
        limit = e.cfg.limits.critique_max_cycles + node.extra_cycles
        fb = {"cycle": plan.cycle, "source": "critique", "text": "critique requested rework: "
              + "; ".join(f"{f.get('severity')}: {f.get('title')}" for f in data.get("findings", [])),
              "attestations": [str(a) for a in r.docs.values()]}
        if plan.cycle >= limit:
            e._escalate(node, "critique_budget", f"G-C2: {plan.cycle} critique cycles without PASS",
                        resume="critique_budget")
            node.feedback.append(fb)
        else:
            plan.cycle += 1
            plan.feedback.append(fb)
            plan.status = REWORK
            node.cycle += 1
            node.status = PENDING
