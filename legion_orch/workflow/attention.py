"""What a human should look at (WORKFLOW.md §2.2): the one definition of "needs you".

Two kinds of item, derived from a change's state alone (no store reads, no model call):

* **blocking** – the workflow waits for a decision owner: a human gate that is
  ``AWAITING_HUMAN`` (H-1, H-E1, H-2, H-3), or an ``ESCALATED`` node. They stay until the decision
  is made; the notifier repeats them while they wait.
* **inbox** – worth reading, but nothing waits for it: a change that ended, a retrospective
  report that is ready. They never resolve; the UI tracks which ones were read.

The UI, the CLI, the change list and the notifier all use :func:`items`, so a count shown
anywhere is the same count.
"""
from __future__ import annotations

from pydantic import BaseModel

from .model import (
    AWAITING_HUMAN,
    ESCALATED,
    HUMAN,
    RETRO,
    SHIP,
    ChangeStatus,
)
from .view import ChangeView

REASON_MAX = 300           # escalation reasons can quote agent output: keep notifications short

GATE, ESCALATION, CHANGE_ENDED, RETROSPECTIVE = "gate", "escalation", "change_ended", "retrospective"
KINDS = (GATE, ESCALATION, CHANGE_ENDED, RETROSPECTIVE)


class AttentionItem(BaseModel):
    change: str
    goal: str
    node: str | None          # the node to open (None: the change as a whole)
    kind: str                 # gate | escalation | change_ended | retrospective
    detail: str               # gate id, escalation kind or the change's final status
    title: str
    reason: str = ""          # escalation reason (truncated)
    since: str = ""           # when it started (ISO); "" when an older store did not record it
    blocking: bool

    @property
    def key(self) -> str:
        """Identity across saves: the same decision raised again later is a new item."""
        return f"{self.change}|{self.node}|{self.kind}|{self.detail}|{self.since}"


def _clip(s: str, n: int = REASON_MAX) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def items(e: ChangeView) -> list[AttentionItem]:
    """Every attention item of one change, blocking first, oldest first within each kind."""
    ch = e.change
    if ch is None:
        return []
    goal = _clip(ch.goal, 120)
    out: list[AttentionItem] = []
    for n in e.nodes.values():
        if n.kind == HUMAN and n.status == AWAITING_HUMAN:
            out.append(AttentionItem(change=ch.id, goal=goal, node=n.id, kind=GATE, detail=n.id,
                                     title=f"{n.id}: {n.title}", since=n.status_since or "",
                                     blocking=True))
        elif n.status == ESCALATED and n.escalation:
            esc = n.escalation
            out.append(AttentionItem(change=ch.id, goal=goal, node=n.id, kind=ESCALATION,
                                     detail=esc.kind, title=f"{n.title}: {esc.kind}",
                                     reason=_clip(esc.reason), since=esc.at, blocking=True))
    if ch.status != ChangeStatus.ACTIVE:
        ship = e.nodes.get(SHIP)
        since = (ch.ended.at if ch.ended else None) or (ship.status_since if ship else None) or ""
        verb = {ChangeStatus.READY: "is ready to ship", ChangeStatus.NOT_READY: "is not ready",
                ChangeStatus.ABANDONED: "was abandoned"}[ch.status]
        out.append(AttentionItem(change=ch.id, goal=goal, node=SHIP if ship and not ch.ended else None,
                                 kind=CHANGE_ENDED, detail=str(ch.status),
                                 title=f"Change {verb}",
                                 reason=_clip(ch.ended.reason or "") if ch.ended else "",
                                 since=since, blocking=False))
    retro = e.nodes.get(RETRO)
    if retro is not None and retro.status == "ACCEPTED":
        out.append(AttentionItem(change=ch.id, goal=goal, node=RETRO, kind=RETROSPECTIVE,
                                 detail=f"cycle-{retro.cycle}", title="Retrospective report ready",
                                 since=retro.status_since or "", blocking=False))
    out.sort(key=lambda i: (not i.blocking, i.since))
    return out
