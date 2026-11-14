"""planner: spec, delta specs, oracle and task graph (G-P1..G-P10)."""
from __future__ import annotations

from .. import guards
from ..model import CRITIQUE, PENDING
from ..view import RoleContext
from .base import Report, RoleHandler, register


@register
class Planner(RoleHandler):
    roles = ("planner",)
    sees_oracle_commit = False

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        tg, e1 = guards.load_json(e.docs, r.docs[e.p("plan/taskgraph.json")], "G-P1")
        orc, e2 = guards.load_json(e.docs, r.docs[e.p("plan/oracle.json")], "G-P1")
        if e1 or e2:
            return e1 + e2
        v = guards.plan(tg, orc, e.change.requirements, e.change.tier, e.cfg.protected_paths,
                        e.assets.roles["implementer"].souls_allowed, e.docs.schemas)
        _, vs = guards.required_docs(r.run.id, [e.p(f"plan/specs/{q}.md") for q in e.change.requirements],
                                     r.parsed, e.docs)
        return v + [x.replace("G-R4", "G-P9") for x in vs]

    async def on_done(self, e: RoleContext, r: Report) -> None:
        await super().on_done(e, r)
        e.nodes[CRITIQUE].status = PENDING       # (re-)critique the new plan
