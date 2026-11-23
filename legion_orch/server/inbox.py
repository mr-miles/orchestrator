"""The inbox: tasks addressed to the cards of a change (an A2A artefact, beside the catalog).

Every item has a ``from`` address set by whoever knows the identity, never by its text: an
agent's card id (from its bearer), ``orchestrator`` (the controller), or ``human:<approver>`` – a
decision owner's word, always carried by the orchestrator after the workflow recorded and applied
it, so a ``human:`` item is a recorded decision. Items:

* **messages** – a sender's text waiting for the recipient's reply (``a2a_reply``);
* **questions back** – a recipient answered with a question (input-required): the sender's to
  answer, which continues the same A2A task.

(The run's own work task – the orchestrator's – is the workflow's: it is shown alongside, from
the engine's record.) The inbox holds no workflow rules: it stores, replies, and **tells the
engine** when something waits on an agent (``Controller.item_added``) – the agent runner starts
a turn for an agent not running. In memory, like the A2A tasks that carry the items.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..util import now_iso
from ..workflow.engine import NoAgent


@dataclass
class InboxItem:
    """A message waiting for its recipient's reply."""
    task_id: str
    card: str                        # the recipient
    sender: str                      # its address: a card id, "orchestrator", "human:<who>"
    sender_role: str
    text: str
    at: str = field(default_factory=now_iso)
    reply: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())

    def view(self) -> dict:
        return {"task_id": self.task_id, "from": self.sender, "from_role": self.sender_role,
                "kind": "message", "state": "working", "text": self.text, "at": self.at}


@dataclass
class QuestionBack:
    """A recipient answered with a question (input-required): the sender's to answer."""
    task_id: str
    card: str                        # who asks
    question: str
    at: str = field(default_factory=now_iso)

    def view(self) -> dict:
        return {"task_id": self.task_id, "from": self.card, "kind": "question",
                "state": "input-required", "text": self.question, "at": self.at,
                "how": "answer with a2a_reply(task_id, text): it continues your conversation "
                       "with " + self.card}


class Inbox:
    """Every change's inbox items, by card. ``tell(cid, card)``: something now waits on that
    card's agent (the engine's runner starts a turn if it is not running)."""

    def __init__(self, tell):
        self.tell = tell
        self._messages: dict[tuple[str, str], dict[str, InboxItem]] = {}
        self._questions: dict[tuple[str, str], dict[str, QuestionBack]] = {}

    # ------------------------------------------------------------------ reading
    def pending(self, cid: str, card: str) -> list[InboxItem]:
        return list(self._messages.get((cid, card), {}).values())

    def questions_for(self, cid: str, card: str) -> list[QuestionBack]:
        return list(self._questions.get((cid, card), {}).values())

    def views(self, cid: str, card: str) -> list[dict]:
        return [i.view() for i in self.pending(cid, card)] + [
            q.view() for q in self.questions_for(cid, card)]

    def question(self, cid: str, card: str, task_id: str) -> QuestionBack | None:
        return self._questions.get((cid, card), {}).get(task_id)

    # ------------------------------------------------------------------ delivering
    async def deliver(self, cid: str, card: str, sender: str, sender_role: str, text: str,
                      task_id: str) -> tuple[str, str]:
        """Put ``text`` in ``card``'s inbox and wait for its reply (state, text). A reply that
        asks something back becomes a question in the sender's inbox."""
        self._questions.get((cid, sender), {}).pop(task_id, None)   # a follow-up: answered
        box = self._messages.setdefault((cid, card), {})
        item = InboxItem(task_id, card, sender, sender_role, text)
        box[task_id] = item
        self.tell(cid, card)
        try:
            state, answer = await item.reply
        finally:
            box.pop(task_id, None)
        if state == "input-required":
            self._questions.setdefault((cid, sender), {})[task_id] = QuestionBack(
                task_id, card, answer)
            self.tell(cid, sender)
        return state, answer

    def reply(self, cid: str, card: str, task_id: str, state: str, text: str) -> bool:
        """The recipient's reply to a message (False: none such waits)."""
        item = self._messages.get((cid, card), {}).get(task_id)
        if item is None or item.reply.done():
            return False
        item.reply.set_result((state, text))
        return True

    def answered(self, cid: str, sender: str, task_id: str) -> None:
        """The sender answered its question back (the A2A layer sends it on)."""
        self._questions.get((cid, sender), {}).pop(task_id, None)

    def turn_ended(self, cid: str, card: str) -> None:
        """``card``'s run is over: what it left waiting is announced again (the agent runner
        answers it in turns of their own)."""
        if any(not i.reply.done() for i in self.pending(cid, card)):
            self.tell(cid, card)

    def fail(self, cid: str, card: str, task_id: str, why: str) -> None:
        """The recipient can no longer answer: the message fails with why."""
        item = self._messages.get((cid, card), {}).get(task_id)
        if item is not None and not item.reply.done():
            item.reply.set_exception(NoAgent(why))

    def forget(self, cid: str) -> None:
        """The change is gone: its messages fail."""
        for (c, card), box in list(self._messages.items()):
            if c == cid:
                for task_id in list(box):
                    self.fail(cid, card, task_id, "the change ended")
