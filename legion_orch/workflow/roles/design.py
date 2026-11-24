"""design review: findings, their review and the selection (G-DR1..G-DR3)."""
from __future__ import annotations

import json

from .. import guards
from ..tickets import Ticket
from ..view import RoleContext
from .base import Report, RoleHandler, doc_of, output, outputs_named, register
from .critic import Critic


def _latest(e: RoleContext, name: str) -> str | None:
    refs = outputs_named(e, name)
    return refs[-1] if refs else None


def _doc(text: str | None) -> dict:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@register
class DesignReviewer(RoleHandler):
    roles = ("design-reviewer",)

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        try:
            doc = json.loads(output(e, r, "findings") or "")
        except ValueError as x:
            return [f"G-DR1: findings is not JSON: {x}"]
        return guards.findings_problems(doc, r.repo_atts)


@register
class FindingsReviewer(Critic):
    roles = ("findings-reviewer",)

    def extra_variables(self, e: RoleContext, ticket: Ticket) -> dict[str, str]:
        return {}

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        v = await super().guards(e, r)
        try:
            review = json.loads(output(e, r, "findings-review") or "")
        except ValueError as x:
            return [*v, f"G-DR2: findings-review is not JSON: {x}"]
        return [*v, *guards.review_problems(review, _doc(doc_of(e, _latest(e, "findings"))))]


@register
class Selector(RoleHandler):
    roles = ("selector",)

    def extra_context(self, e: RoleContext, ticket: Ticket) -> list[str]:
        return [ref for ref in (_latest(e, "findings"), _latest(e, "findings-review")) if ref]

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        try:
            selection = json.loads(output(e, r, "selection") or "")
        except ValueError as x:
            return [f"G-DR3: selection is not JSON: {x}"]
        return guards.selection_problems(selection, _doc(doc_of(e, _latest(e, "findings"))),
                                         _doc(doc_of(e, _latest(e, "findings-review"))))
