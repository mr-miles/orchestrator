"""Every card's inbox: what waits on each agent of the change, and how it is delivered.

An agent's inbox (MCP ``inbox()``) holds every task waiting on it:

* **its work** – the run's task, the orchestrator's (:class:`RunTasks`, below);
* **messages** other agents sent it, each waiting for its reply (:class:`InboxItem`);
* **questions back** – an agent it messaged answered with a question (:class:`QuestionBack`).

``a2a_reply(task_id, text, state)`` ends or answers any of them (:meth:`Inboxes.reply_from`).
Delivery (:meth:`Inboxes.deliver`): to an agent with a live run, its inbox; to one not
running, a resumed turn of its session; to one not started, a consult that starts it. The A2A
layer (``server/a2a.py``) only carries these as A2A tasks. Messages live in memory, as the A2A
tasks carrying them do.

A run is an A2A task: the orchestrator's, on the agent's card, in the agent's one inbox.

* Its id is the run's; its first message is the rendered assignment (``Run.instructions``, which
  the prompt pin covers); a rework is a new run, so a new task in the same context (the step).
* ``report(...)`` **records** – documents, attestations, structured data – and no longer ends
  anything. The agent ends (or pauses) the task with ``a2a_reply(task_id, text, state)``:

  ========================  =====================================  ==============================
  reply state               needs                                  then
  ========================  =====================================  ==============================
  ``completed``             an accepted ``DONE`` report            the run concludes: its step goes on
  ``input-required``        an accepted ``BLOCKED`` report         the run concludes: its step escalates
  ``failed``/``rejected``   –                                      the run fails (the agent gave up)
  ========================  =====================================  ==============================

* The reply is always taken. When it does not match the run's record, the orchestrator **asks
  again** – the next message on the task says what is missing – and so it does when the agent's
  turn ends with the task still open (its session is resumed for another turn of the same run).
  Each ask-again uses one report attempt (G-R6): when none is left the run fails.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .....util import now_iso
from .... import cards
from ....model import LIVE_RUN, R_FAILED, R_REPORTED, FailureSource, Run, TaskReply
from ...base.common import AgentBusy, NoAgent
from ...base.core import ChangeCore
from ..runs import RunLifecycle

# a turn of an agent not running, answering a message: (node, role, sender, text) -> its answer
Turn = Callable[[str, str, str, str], Awaitable[str]]

STATES = ("completed", "input-required", "failed", "rejected")


class RunTasks:
    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle):
        self.c, self.lifecycle = core, lifecycle

    # ------------------------------------------------------------------ the record's state
    @staticmethod
    def wanted(run: Run) -> str | None:
        """The reply the run's record asks for: completed (an accepted DONE report),
        input-required (an accepted BLOCKED one), None (nothing accepted yet)."""
        if run.status != R_REPORTED or run.report is None:
            return None
        return "input-required" if str(run.report.status) == "BLOCKED" else "completed"

    @classmethod
    def ended(cls, run: Run) -> bool:
        """The task is over: replied as the record asks, or the run failed."""
        if run.status == R_FAILED:
            return True
        return run.reply is not None and run.reply.state == cls.wanted(run)

    def state(self, run: Run) -> str:
        if run.status == R_FAILED:
            return "failed"
        if self.ended(run):
            return run.reply.state
        return "working"

    # ------------------------------------------------------------------ the agent replies
    def reply(self, run: Run, state: str, text: str,
              unanswered: list[tuple[str, str]] = ()) -> dict:
        """Under the lock: the agent's ``a2a_reply`` on its run's task. Always taken; a reply
        the record does not support is answered with what is missing (an ask-again). Ending it
        with messages still in its inbox (``unanswered``: (task id, sender)) is answered with how
        many wait – but only when one arrived it was not told of yet, so it cannot loop; what is
        left when it ends is answered in a turn of its own after the run."""
        if state not in STATES:
            return {"ok": False, "error": f"state must be one of {', '.join(STATES)}"}
        if self.ended(run):
            return {"ok": True, "state": self.state(run), "note": "the task was ended already"}
        new = [(tid, who) for tid, who in unanswered if tid not in run.inbox_reminded]
        if new and state in ("completed", "input-required") and state == self.wanted(run):
            run.inbox_reminded += [tid for tid, _ in new]
            n = len(unanswered)
            msg = (f"{n} task{'s' if n != 1 else ''} in your inbox (listed in `inbox`) – answer "
                   "with a2a_reply, then end your task again.")
            run.asked.append(msg)
            self.c.event("agent", f"asked to answer {len(new)} message(s) first",
                         run.node_id, run.id)
            self.c.save()
            return {"ok": False, "state": "working", "message": msg}
        if state in ("failed", "rejected"):
            run.reply = TaskReply(state=state, text=text.strip())
            self.c.event("agent", f"the agent ended its task {state}: {text.strip()[:300]}",
                         run.node_id, run.id)
            self.lifecycle.fail_run(run, f"the agent gave up ({state}): {text.strip()[:500]}",
                                    FailureSource.AGENT)
            self.c.save()
            return {"ok": True, "state": "failed"}
        if state == self.wanted(run):                # only a reply the record supports ends it
            run.reply = TaskReply(state=state, text=text.strip())
            self.c.event("agent", f"task {state}", run.node_id, run.id)
            self.c.save()
            return {"ok": True, "state": state,
                    "next": "end your turn: your work goes back to the workflow"}
        msg = self.ask_again(run, f"You replied {state}, but")
        self.c.save()
        return {"ok": False, "state": self.state(run), "message": msg}

    def ask_again(self, run: Run, opening: str | None = None) -> str | None:
        """Under the lock: the orchestrator's next message on the task – what is missing – using
        one report attempt (None, and the run failed: none is left)."""
        msg = self._missing(run, opening)
        limit = self.c.cfg.limits.max_report_attempts
        if run.attempts >= limit:
            if run.status in LIVE_RUN:
                self.lifecycle.fail_run(run, f"the task was not ended after {limit} attempts: "
                                             + msg, FailureSource.AGENT)
            return None
        run.attempts += 1
        run.asked.append(msg)
        self.c.event("agent", f"asked again ({run.attempts}/{limit}): {msg[:200]}",
                     run.node_id, run.id)
        return msg

    def _missing(self, run: Run, opening: str | None) -> str:
        wanted = self.wanted(run)
        said = f"{opening} " if opening else "Your task is still open: "
        if wanted is None:
            last = "; ".join(run.violations[-1]) if run.violations else ""
            return (said + "this run has no accepted report. File your results with "
                    "report(...) (status DONE, or BLOCKED with blocked_reason and "
                    "missing_decision)" + (f" – your last report was rejected: {last[:800]}"
                                           if last else "") +
                    ", then end the task with a2a_reply(task_id, text, state).")
        return (said + f"your accepted report is {run.report.status}: end the task with "
                f"a2a_reply(task_id=\"{run.id}\", text=..., state=\"{wanted}\").")

    # ------------------------------------------------------------------ the agent's view
    def view(self, run: Run) -> dict:
        """The task as the agent's inbox shows it."""
        return {"task_id": run.id, "from": "orchestrator", "kind": "work",
                "state": self.state(run), "cycle": run.cycle,
                "messages": [{"from": "orchestrator", "text": run.instructions}]
                + [{"from": "orchestrator", "text": m} for m in run.asked]
                + ([{"from": "you", "text": run.reply.text, "state": run.reply.state}]
                   if run.reply else []),
                "how_to_end": "report(...) records your results; then a2a_reply(task_id, text, "
                              "state): completed after an accepted DONE report, "
                              "input-required after an accepted BLOCKED one, failed to give up",
                "at": run.created_at, "now": now_iso()}


# ---------------------------------------------------------------------------- every card's inbox
@dataclass
class InboxItem:
    """A message waiting for an agent's reply."""
    task_id: str
    card: str
    sender: str                      # its card id
    sender_role: str
    text: str
    at: str = field(default_factory=now_iso)
    reply: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())

    def view(self) -> dict:
        return {"task_id": self.task_id, "from": self.sender, "from_role": self.sender_role,
                "kind": "message", "state": "working", "text": self.text, "at": self.at}


@dataclass
class QuestionBack:
    """An agent answered a message with a question (input-required): the sender's to answer."""
    task_id: str
    card: str                        # who asks
    question: str
    at: str = field(default_factory=now_iso)

    def view(self) -> dict:
        return {"task_id": self.task_id, "from": self.card, "kind": "question",
                "state": "input-required", "text": self.question, "at": self.at,
                "how": "answer with a2a_reply(task_id, text): it continues your conversation "
                       "with " + self.card}


class Inboxes:
    """The change's inboxes, one per card: run tasks, messages, questions back."""

    WAIT_S = 5.0                     # longest wait between looks at a recipient's state

    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle, tasks: RunTasks, turn: Turn):
        self.c, self.lifecycle, self.tasks, self.turn = core, lifecycle, tasks, turn
        self._messages: dict[str, dict[str, InboxItem]] = {}       # card -> task id -> item
        self._questions: dict[str, dict[str, QuestionBack]] = {}   # sender card -> asked

    # ------------------------------------------------------------------ an agent's view
    def pending(self, card: str) -> list[InboxItem]:
        return list(self._messages.get(card, {}).values())

    def questions_for(self, card: str) -> list[QuestionBack]:
        return list(self._questions.get(card, {}).values())

    def view(self, run: Run, assignment: dict) -> list[dict]:
        """Everything in ``run``'s agent's inbox: its work first, then messages, questions."""
        card = cards.card_id(run.role, run.node_id)
        return [self.tasks.view(run) | {"assignment": assignment},
                *(i.view() for i in self.pending(card)),
                *(q.view() for q in self.questions_for(card))]

    def remaining(self, run: Run) -> list[dict]:
        """What is still waiting on it (returned with every reply: no need to ask again)."""
        card = cards.card_id(run.role, run.node_id)
        work = self.tasks.view(run)
        own = ([{"task_id": work["task_id"], "from": "orchestrator", "kind": "work",
                 "state": work["state"], "last": work["messages"][-1]["text"][:500]}]
               if work["state"] == "working" else [])
        return own + [i.view() | {"text": i.text[:2000]} for i in self.pending(card)] + [
            q.view() for q in self.questions_for(card)]

    # ------------------------------------------------------------------ the agent replies
    async def reply_from(self, run: Run, task_id: str, text: str, state: str) -> dict:
        """``a2a_reply``: its work's task, a message's, or a question back's – with what is
        still in its inbox."""
        card = cards.card_id(run.role, run.node_id)
        if task_id == run.id:
            waiting = [(i.task_id, i.sender) for i in self.pending(card)]
            async with self.c.lock:
                out = self.tasks.reply(run, state, text, waiting)
            return out | {"inbox": self.remaining(run)}
        text = text.strip()
        if not text:
            return {"ok": False, "error": "text is required", "inbox": self.remaining(run)}
        if task_id in self._questions.get(card, {}):
            await self.c.host.follow_up(self.c.cid, card, task_id, text)
            self._questions[card].pop(task_id, None)
            self.c.event("a2a", f"{card} answered a question back", run.node_id, run.id)
            return {"ok": True, "state": "sent: the conversation goes on",
                    "inbox": self.remaining(run)}
        if state not in ("completed", "input-required", "rejected"):
            return {"ok": False, "error": "a message's reply state is completed, "
                                          "input-required or rejected",
                    "inbox": self.remaining(run)}
        item = self._messages.get(card, {}).pop(task_id, None)
        if item is None or item.reply.done():
            return {"ok": False, "error": f"no task {task_id} waits in your inbox (answered "
                                          "already, or its sender cancelled it)",
                    "inbox": self.remaining(run)}
        item.reply.set_result((state, text))
        self.c.event("a2a", f"{card} replied {state} to a message", run.node_id, run.id)
        return {"ok": True, "state": state, "inbox": self.remaining(run)}

    # ------------------------------------------------------------------ delivery
    async def deliver(self, card: cards.Card, sender: str, sender_role: str, text: str,
                      task_id: str) -> tuple[str, str]:
        """``text`` from ``sender`` to ``card``'s agent; its reply (state, text). While it has a
        live run the message waits in its inbox; otherwise – not running, or its run ended
        without answering – a turn of its session answers (a consult if it has not started).
        Raises :class:`NoAgent` once it cannot answer any more. A question back it asks is
        the sender's (:meth:`questions_for`)."""
        self._questions.get(sender, {}).pop(task_id, None)     # a follow-up: answered
        box = self._messages.setdefault(card.id, {})
        item = InboxItem(task_id, card.id, sender, sender_role, text)
        try:
            state, answer = await self._deliver(card, box, item)
        finally:
            box.pop(task_id, None)
        if state == "input-required":
            self._questions.setdefault(sender, {})[task_id] = QuestionBack(task_id, card.id,
                                                                           answer)
        return state, answer

    async def _deliver(self, card, box, item) -> tuple[str, str]:
        node, role, c = card.node, card.role, self.c
        while True:
            state = self.lifecycle.agent_state(node, role)
            if state is None:
                raise NoAgent(f"{card.id} can no longer answer (its step is over)")
            if state == "live":
                if item.task_id not in box and not item.reply.done():
                    box[item.task_id] = item
                    c.event("a2a", f"message from {item.sender} waits in {card.id}'s inbox",
                            node if node in c.nodes else None)
                saved = asyncio.ensure_future(c.next_save())
                try:
                    await asyncio.wait({item.reply, saved}, timeout=self.WAIT_S,
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    saved.cancel()
                if item.reply.done():
                    return item.reply.result()
                continue
            box.pop(item.task_id, None)              # not running: a turn answers
            if item.reply.done():
                return item.reply.result()
            try:
                return "completed", await self.turn(node, role, item.sender, item.text)
            except AgentBusy:                        # a run started meanwhile: its inbox
                await asyncio.sleep(0.2)
