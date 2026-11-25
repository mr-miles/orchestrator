"""intake: the requirements and the risk assessment (G-I1, G-I2), from the goal."""
from __future__ import annotations

import json

from .. import guards
from ..model import ReportStatus
from ..view import RoleContext
from .base import Report, RoleHandler, output, pointer, register


@register
class Intake(RoleHandler):
    roles = ("intake",)

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        req, risk = output(e, r, "requirements"), output(e, r, "risk")
        try:
            rj = json.loads(risk or "")
        except ValueError as x:
            return [f"G-I2: risk is not JSON: {x}"]
        v = guards.intake(req or "", rj, None, ReportStatus.DONE)
        if sel := pointer(e, "selection"):
            try:
                v += guards.requirements_problems(req or "", json.loads(sel))
            except ValueError as x:
                v.append(f"G-DR4: selection is not JSON: {x}")
        return v
