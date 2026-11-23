"""integration: the whole change, integrated – the oracle's commands at the head, the protected
tests unchanged (G-INT1, G-INT2); its verdict is its own work's outcome (a FAIL: its ticket
reworks or, at the limit, escalates)."""
from __future__ import annotations

import json

from .. import guards
from ..model import Oracle, Verdict
from ..view import RoleContext
from .base import Report, RoleHandler, Said, outputs_named, pointer, protected_unchanged, register


@register
class Integration(RoleHandler):
    roles = ("integration",)

    def extra_context(self, e: RoleContext, ticket) -> list[str]:
        """Every build's evidence and (R3) the baseline: the oracle's commands before any build."""
        return outputs_named(e, "evidence") + outputs_named(e, "baseline", "controller:baseline")

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        orc = Oracle.model_validate(json.loads(pointer(e, "oracle") or "{}"))
        v = guards.verdict(r.data, cycle=2, require_checked=False)
        v += guards.findings(r.data.findings, r.repo_atts, gid="G-INT1")
        v += guards.results_cover(r.data, orc.acceptance_commands + orc.integration_commands,
                                  "G-INT1")
        return v + await protected_unchanged(e, r.sha)

    async def on_done(self, e: RoleContext, r: Report) -> Said:
        return Said(r.data.verdict, () if r.data.verdict == Verdict.PASS else tuple(
            f"[{f.severity}] {f.title}: {f.observation}" for f in r.data.findings)[:30])
