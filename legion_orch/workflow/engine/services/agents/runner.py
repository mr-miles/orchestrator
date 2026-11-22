"""The agent runner: what the workflow talks to about agents (~/plans/inbox-runner.md).

* The workflow tells it when the change and its steps open and close: it publishes and
  withdraws their agents' cards (to the A2A catalog, through the host).
* The A2A inbox tells it when something waits on an agent (:meth:`item_added`), and the
  agent's own run tells it when its turn ends (:meth:`turn_ended`): for an agent not running
  it takes the waiting messages one by one – a consult if it has not started (always for the
  resolver), else a resumed turn of its session – each in a dispatcher slot, so
  ``limits.max_parallel`` counts it, and posts the agent's answer as its reply. A running agent
  reads its inbox itself (MCP ``inbox()``).

(A step's own work is still launched by its phases; that moves here next.)
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .... import cards
from ....model import Node
from ...base.common import NoAgent, WorkflowError
from ...base.core import ChangeCore
from ...base.dispatch import Dispatcher
from ...sandboxes.sandboxes import Sandboxes
from ..runs import RunLifecycle

if TYPE_CHECKING:
    from ..agent_runs import AgentRuns


class _TurnWork:
    """An agent's turn outside its step's work, as the dispatcher's work: it starts once the
    role's environment is built, in a free slot."""

    def __init__(self, ready, run):
        self.can_start, self._run = ready, run

    async def execute(self):
        return await self._run()


class AgentRunner:
    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle, agents: AgentRuns,
                 sandboxes: Sandboxes, dispatcher: Dispatcher):
        self.c, self.lifecycle, self.agents = core, lifecycle, agents
        self.sandboxes, self.dispatcher = sandboxes, dispatcher
        self._busy: set[str] = set()          # cards whose messages are being answered

    # ------------------------------------------------------------------ the workflow tells it
    def open_change(self) -> None:
        """The change started: its own agents' cards (orchestrator, resolver)."""
        self._publish(cards.change_cards(self.c.cid))

    def close_change(self) -> None:
        """The change ended: every card goes."""
        self._remove(None)

    def open_step(self, node: Node) -> None:
        """A step started (again, for a new cycle): its agents' cards (``node.agent_roles``)."""
        self._publish(cards.step_cards(node))

    def close_step(self, node_id: str) -> None:
        """A step ended – accepted, sent back, ended, or its node dropped: its cards go."""
        self._remove(node_id)

    def republish(self) -> None:
        """A catalog subscribed (a restart): every card the workflow has open, again."""
        cs = cards.cards(self.c)
        if cs:
            self.c.host.publish_cards(self.c.cid, cs)

    def _publish(self, new: list) -> None:
        if not new:
            return
        self.c.host.publish_cards(self.c.cid, new)
        for card in new:
            self.c.event("a2a", f"card registered: {card.id}",
                         card.node if card.node in self.c.nodes else None)

    def _remove(self, node_id: str | None) -> None:
        self.c.host.remove_cards(self.c.cid, node_id)
        self.c.event("a2a", f"cards removed: {node_id or 'all of the change'}",
                     node_id if node_id in self.c.nodes else None)

    # ------------------------------------------------------------------ inbox events
    def item_added(self, card_id: str) -> None:
        """Something waits in ``card_id``'s inbox: answer it if its agent is not running."""
        card = next((c for c in self.c.a2a_cards() if c.id == card_id), None)
        if card is None or card.node is None or card_id in self._busy:
            return
        if self.lifecycle.agent_state(card.node, card.role) == "live":
            return                             # it reads its inbox itself; turn_ended follows
        self._busy.add(card_id)
        self.c.host.spawn(self._answer_all(card))

    def turn_ended(self, node_id: str, role: str) -> None:
        """A run's turn ended: what it left unanswered is answered in turns of its own."""
        self.item_added(cards.card_id(role, node_id))

    async def _answer_all(self, card) -> None:
        try:
            while True:
                waiting = self.c.host.inbox_messages(self.c.cid, card.id)
                if not waiting:
                    return
                if self.lifecycle.agent_state(card.node, card.role) == "live":
                    return                     # a run started: it reads them
                task_id, sender, text = waiting[0]
                try:
                    answer = await self.turn(card.node, card.role, sender, text)
                except (NoAgent, WorkflowError) as e:
                    self.c.host.fail_message(self.c.cid, card.id, task_id, str(e))
                else:
                    self.c.host.reply_message(self.c.cid, card.id, task_id, "completed", answer)
                await asyncio.sleep(0)             # its sender takes it out of the inbox
        finally:
            self._busy.discard(card.id)

    # ------------------------------------------------------------------ turns
    async def turn(self, node_id: str, role: str, sender: str, text: str) -> str:
        """An agent not running answers ``text``: a consult when it has not started (always,
        for the resolver: it investigates on every message), else one resumed turn of its
        session – in a dispatcher slot, like every sandbox."""
        if self.lifecycle.agent_state(node_id, role) == "absent" or role == cards.RESOLVER:
            run = lambda: self.agents.consult(node_id, role, sender, text)  # noqa: E731
        else:
            run = lambda: self.sandboxes.answerer.answer(node_id, role, sender, text)  # noqa: E731
        ready = lambda: self.sandboxes.environment_built(role)  # noqa: E731
        return await self.dispatcher.run(_TurnWork(ready, run))
