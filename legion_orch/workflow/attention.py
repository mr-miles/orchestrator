"""What a human should look at (WORKFLOW.md §2.2): the one definition of "needs you".

Two kinds of item, derived from a change's state alone (no store reads, no model call):

* **blocking** – the workflow waits for a decision owner: a sign-off under way, a ticket waiting
  on a human (an escalation: why, its findings, the resolver's proposal), an agent's open
  question (A2A ``ask_human``), a paused workflow. They stay until the decision is made; the
  notifier repeats them while they wait.
* **inbox** – worth reading, but nothing waits for it: a change that ended, a retrospective's
  report. They never resolve; the UI tracks which ones were read.

The UI, the CLI, the change list and the notifier all use :func:`items`, so a count shown
anywhere is the same count.
"""
from __future__ import annotations

from pydantic import BaseModel, computed_field

from .model import ChangeStatus
from .tickets import Status
from .view import ChangeView

REASON_MAX = 300           # escalation reasons can quote agent output: keep notifications short

GATE, ESCALATION, PAUSED = "gate", "escalation", "paused"
CHANGE_ENDED, QUESTION, RETROSPECTIVE = "change_ended", "question", "retrospective"
KINDS = (GATE, ESCALATION, PAUSED, QUESTION, CHANGE_ENDED, RETROSPECTIVE)


class AttentionItem(BaseModel):
    change: str
    goal: str
    ticket: str | None        # the ticket to open (None: the change as a whole)
    kind: str                 # one of KINDS
    detail: str               # what it is about: a reason for waiting, a question id, a status
    title: str
    reason: str = ""          # why (truncated)
    since: str = ""           # when it started (ISO)
    blocking: bool
    proposal: str = ""        # an escalation: the resolver's proposal, in one line

    @computed_field
    @property
    def key(self) -> str:
        """Identity across saves: the same decision raised again later is a new item."""
        return f"{self.change}|{self.ticket}|{self.kind}|{self.detail}|{self.since}"


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
    tickets = e.tickets.t                 # none before the change has its skeleton
    for t in tickets.values():
        if t.type == "human:signoff" and t.status == Status.WORKING:
            out.append(AttentionItem(change=ch.id, goal=goal, ticket=t.id, kind=GATE,
                                     detail=t.id, title=f"{t.id}: sign off {', '.join(t.signs)}",
                                     since=t.since, blocking=True))
        elif t.status == Status.WAITING:
            prop = ch.proposals.get(t.id)
            why = "; ".join(t.findings) or str(t.waiting)
            out.append(AttentionItem(change=ch.id, goal=goal, ticket=t.id, kind=ESCALATION,
                                     detail=str(t.waiting), title=f"{t.id}: {t.waiting}",
                                     reason=_clip(why), since=t.since, blocking=True,
                                     proposal=_clip(f"{prop.action}: {prop.rationale}", 200)
                                     if prop else ""))
    held = [r for r in (e.tickets.root.paused if tickets else []) if not r.startswith("signing ")]
    if held and ch.status == ChangeStatus.ACTIVE:      # a sign-off's own pause is its item
        root = e.tickets.root
        out.append(AttentionItem(change=ch.id, goal=goal, ticket=None, kind=PAUSED,
                                 detail=",".join(held), title="The workflow is paused",
                                 reason=_clip(", ".join(held)), since=root.since, blocking=True))
    for q in ch.questions.values():
        if q.open:
            out.append(AttentionItem(change=ch.id, goal=goal, ticket=q.ticket, kind=QUESTION,
                                     detail=q.id, title=f"{q.ticket} asks", reason=_clip(q.question),
                                     since=q.asked_at, blocking=True))
    if ch.status != ChangeStatus.ACTIVE:
        verb = {ChangeStatus.READY: "was published", ChangeStatus.ABANDONED: "was abandoned"}[ch.status]
        out.append(AttentionItem(change=ch.id, goal=goal, ticket=None, kind=CHANGE_ENDED,
                                 detail=str(ch.status), title=f"Change {verb}",
                                 reason=_clip(ch.ended.reason or "") if ch.ended else "",
                                 since=(ch.ended.at if ch.ended else "") or "", blocking=False))
    for t in e.hooks.values():               # a side process's result: the retrospective's
        if t.type == f"agent:{RETROSPECTIVE}" and t.status == Status.PASSED:
            out.append(AttentionItem(change=ch.id, goal=goal, ticket=t.id, kind=RETROSPECTIVE,
                                     detail=f"attempt-{t.attempt}", title="Retrospective report",
                                     since=t.since, blocking=False))
    out.sort(key=lambda i: (not i.blocking, i.since))
    return out
