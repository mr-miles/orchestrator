"""intake: requirements and risk signals (G-I1, G-I2, G-I3)."""
from __future__ import annotations

from .. import guards, risk
from ..model import ReportStatus
from ..view import RoleContext
from .base import Report, RoleHandler, register


@register
class Intake(RoleHandler):
    roles = ("intake",)
    sees_requirements = False
    sees_oracle_commit = False

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        req_path = e.p("intake/requirements.md")
        req = e.docs.read(req_path, r.docs[req_path].version)
        rj, err = guards.load_json(e.docs, r.docs[e.p("intake/risk.json")], "G-I2")
        if err:
            return err
        q = e.doc("intake/questions.md").content if e.latest("intake/questions.md") else None
        return guards.intake(req.content, rj, q, ReportStatus.DONE)

    async def on_done(self, e: RoleContext, r: Report) -> None:
        rj = e.json_doc("intake/risk.json", r.docs[e.p("intake/risk.json")].version)
        e.change.risk = risk.derive_tier(rj)
        e.event("risk", f"derived tier {e.change.risk.tier} (score {e.change.risk.total})",
                 r.node.id)
        await super().on_done(e, r)
