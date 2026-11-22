"""The agent runner: what the workflow talks to about agents (~/plans/inbox-runner.md).

The workflow tells it when the change and its steps open and close; it publishes the agents'
cards to the A2A catalog accordingly (through the host), owns their inboxes, and runs every turn
an agent takes outside its step's own work – answering a message, a consult, the resolver's –
through the dispatcher, so ``limits.max_parallel`` counts them like any other sandbox.

(Phase 1: a step's own work is still launched by its phases; it moves here next.)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .... import cards
from ....model import Node
from ...base.core import ChangeCore
from ...base.dispatch import Dispatcher
from ...sandboxes.sandboxes import Sandboxes
from ..runs import RunLifecycle
from .inbox import Inboxes, RunTasks

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
                 sandboxes: Sandboxes, dispatcher: Dispatcher, tasks: RunTasks):
        self.c, self.lifecycle, self.agents = core, lifecycle, agents
        self.sandboxes, self.dispatcher = sandboxes, dispatcher
        self.inboxes = Inboxes(core, lifecycle, tasks, self.turn)

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
