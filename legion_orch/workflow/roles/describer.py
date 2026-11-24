"""describer: the pull request's description, faithful to the change (G-PR1); pr-reviewer: a
judge of it (G-PR2)."""
from __future__ import annotations

from .. import guards
from ..model import Verdict
from ..view import RoleContext
from .base import Report, RoleHandler, Said, judged, output, outputs_named, register, requirement_ids


def _change_context(e: RoleContext) -> list[str]:
    """What a description must be faithful to: the goal, the plan, each build's evidence and
    the integration report."""
    goal = e.latest_att("control/goal.md")
    plan = [e.tickets.root.pointers.get(n) for n in ("spec", "tickets")]
    return [a for a in (goal, *plan) if isinstance(a, str)] + outputs_named(e, "evidence") \
        + outputs_named(e, "report", "agent:integration")


@register
class Describer(RoleHandler):
    roles = ("describer",)

    def extra_context(self, e: RoleContext, ticket) -> list[str]:
        return _change_context(e)

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        return guards.pr_description(output(e, r, "pr") or "", requirement_ids(e))


@register
class PrReviewer(RoleHandler):
    roles = ("pr-reviewer",)

    def extra_context(self, e: RoleContext, ticket) -> list[str]:
        owner = e.tickets.t.get(ticket.parent) if ticket.parent else None
        pr = owner.outputs.get("pr") if owner else None
        return _change_context(e) + ([pr] if isinstance(pr, str) else [])

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        v = []
        if r.data.verdict == Verdict.REWORK and not r.data.findings:
            v.append("G-PR2: REWORK requires at least one finding")
        return v + guards.findings(r.data.findings, r.repo_atts, gid="G-PR2")

    async def on_done(self, e: RoleContext, r: Report) -> Said:
        return judged(r.data.verdict, r.data.findings)
