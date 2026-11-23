"""intake: the requirements and the risk assessment (G-I1, G-I2), from the goal."""
from __future__ import annotations

import json

from .. import guards
from ..model import ReportStatus
from ..view import RoleContext
from .base import Report, RoleHandler, output, register


@register
class Intake(RoleHandler):
    roles = ("intake",)

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        req, risk = output(e, r, "requirements"), output(e, r, "risk")
        try:
            rj = json.loads(risk or "")
        except ValueError as x:
            return [f"G-I2: risk is not JSON: {x}"]
        return guards.intake(req or "", rj, None, ReportStatus.DONE)
