"""baseline: what passes and fails before any build (G-BL1)."""
from __future__ import annotations

import json

from .. import guards
from ..model import Oracle
from ..view import RoleContext
from .base import Report, RoleHandler, pointer, register


@register
class Baseline(RoleHandler):
    roles = ("baseline",)

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        orc = Oracle.model_validate(json.loads(pointer(e, "oracle") or "{}"))
        return guards.results_cover(r.data, orc.acceptance_commands
                                    + orc.integration_commands, "G-BL1")
