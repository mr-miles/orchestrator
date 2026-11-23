"""What a human should look at (WORKFLOW.md §2.2): the one definition of "needs you".

Two kinds of item, derived from a change's state alone (no store reads, no model call):

* **blocking** – the workflow waits for a decision owner: a human gate that is
  ``AWAITING_HUMAN`` (H-1, H-E1, H-2, H-3), an ``ESCALATED`` node, an agent's open question
  (A2A ``ask_human``), or H-3 accepted (or, at
  R0/R1, automatic) but not ready: its preconditions, the ship gates, are not all satisfied
  (they wait for attestations, waivers or a re-check). They
  stay until the decision is made; the notifier repeats them while they wait.
* **inbox** – worth reading, but nothing waits for it: a change that ended, a retrospective
  report that is ready. They never resolve; the UI tracks which ones were read.

The UI, the CLI, the change list and the notifier all use :func:`items`, so a count shown
anywhere is the same count.
"""
from __future__ import annotations

from pydantic import BaseModel

from . import risk
from .model import (
    AWAITING_HUMAN,
    ESCALATED,
    H_CHANGE,
    RETRO,
    ChangeStatus,
    GateStatus,
)
from .view import ChangeView

REASON_MAX = 300           # escalation reasons can quote agent output: keep notifications short

GATE, ESCALATION, NOT_READY = "gate", "escalation", "not_ready"
CHANGE_ENDED, RETROSPECTIVE = "change_ended", "retrospective"
QUESTION = "question"
KINDS = (GATE, ESCALATION, NOT_READY, QUESTION, CHANGE_ENDED, RETROSPECTIVE)


class AttentionItem(BaseModel):
    change: str
    goal: str
    node: str | None          # the node to open (None: the change as a whole)
    kind: str                 # gate | escalation | not_ready | question | change_ended | retrospective
    detail: str               # gate id, escalation kind, unsatisfied gates, question id or final status
    title: str
    reason: str = ""          # escalation reason (truncated)
    since: str = ""           # when it started (ISO); "" when an older store did not record it
    blocking: bool
    proposal: str = ""        # an escalation: the resolver's proposal, in one line

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
        if n.status == AWAITING_HUMAN and n.id == H_CHANGE and ch.ship \
                and (H_CHANGE in ch.approvals or not ch.tier or risk.rank(ch.tier) < 2):
            bad = {g: r for g, r in ch.ship.gates.items() if r.status != GateStatus.SATISFIED}
            out.append(AttentionItem(
                change=ch.id, goal=goal, node=n.id, kind=NOT_READY, detail=",".join(sorted(bad)),
                title=f"Not ready to ship: {len(bad)} gate{'s' * (len(bad) != 1)} not satisfied",
                reason=_clip("; ".join(f"{g}: {r.detail}" for g, r in sorted(bad.items()))),
                since=n.status_since or "", blocking=True))
        elif n.status == AWAITING_HUMAN:
            out.append(AttentionItem(change=ch.id, goal=goal, node=n.id, kind=GATE, detail=n.id,
                                     title=f"{n.id}: {n.title}", since=n.status_since or "",
                                     blocking=True))
        elif n.status == ESCALATED and n.escalation:
            esc = n.escalation
            prop = esc.proposal
            out.append(AttentionItem(change=ch.id, goal=goal, node=n.id, kind=ESCALATION,
                                     detail=esc.kind, title=f"{n.title}: {esc.kind}",
                                     reason=_clip(esc.reason), since=esc.at, blocking=True,
                                     proposal=_clip(f"{prop.action}: {prop.rationale}", 200)
                                     if prop else ""))
    for q in ch.questions.values():
        if q.open:
            out.append(AttentionItem(change=ch.id, goal=goal, node=q.node, kind=QUESTION,
                                     detail=q.id, title=f"{q.node} asks", reason=_clip(q.question),
                                     since=q.asked_at, blocking=True))
    if ch.status != ChangeStatus.ACTIVE:
        h3 = e.nodes.get(H_CHANGE)
        since = (ch.ended.at if ch.ended else None) or (h3.status_since if h3 else None) or ""
        verb = {ChangeStatus.READY: "is ready to ship", ChangeStatus.NOT_READY: "is not ready",
                ChangeStatus.ABANDONED: "was abandoned"}[ch.status]
        out.append(AttentionItem(change=ch.id, goal=goal, node=H_CHANGE if h3 and not ch.ended else None,
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
