"""The agent runner: what the workflow talks to about agents (~/plans/inbox-runner.md).

* The workflow tells it when the change and its steps open and close.
* Cards travel on the bus (``CardsPublished`` / ``CardsWithdrawn``, after the change saves);
  the A2A catalog subscribes.
* The A2A inbox's ``ItemAdded`` (a bus event) tells it when something waits on an agent – also
  when a run's turns end with messages left in its inbox: for an agent not running
  it takes the waiting messages one by one – a consult if it has not started (always for the
  resolver), else a resumed turn of its session – each in a dispatcher slot, so
  ``limits.max_parallel`` counts it, and posts the agent's answer as its reply. A running agent
  reads its inbox itself (MCP ``inbox()``).

* The workflow's own work: a phase posts it (:meth:`publish_work`) and the runner launches it –
  placement, the working copy, the turns – in the step's dispatcher slot, and returns how it
  went; the phase decides what it means.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from .....bus import CardsPublished, CardsWithdrawn, ItemAdded
from .... import cards
from ....model import Node, Run
from ...base.common import NoAgent, WorkflowError
from ...base.core import ChangeCore
from ...base.dispatch import Dispatcher
from ...sandboxes.plan import SandboxPlan
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
        core.svc.bus.subscribe(ItemAdded, lambda e: self.item_added(e.card), cid=core.cid)

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
        """A catalog subscribed (a restart): every card the workflow has open, again – at once
        (no change is being saved)."""
        cs = cards.cards(self.c)
        if cs:
            self.c.svc.bus.publish(CardsPublished(self.c.cid, tuple(cs), replay=True))

    def _publish(self, new: list) -> None:
        if not new:
            return
        self.c.emit(CardsPublished(self.c.cid, tuple(new)))
        for card in new:
            self.c.event("a2a", f"card registered: {card.id}",
                         card.node if card.node in self.c.nodes else None)

    def _remove(self, node_id: str | None) -> None:
        self.c.emit(CardsWithdrawn(self.c.cid, node_id))
        self.c.event("a2a", f"cards removed: {node_id or 'all of the change'}",
                     node_id if node_id in self.c.nodes else None)

    # ------------------------------------------------------------------ the workflow's work
    async def publish_work(self, node: Node, role: str, plan: SandboxPlan,
                           judged_sha: str | None, *, wanted: Callable[[], bool],
                           started: Callable[[Run], None], dispatch):
        """A step's work for its ``role`` agent – its run's task, in the agent's inbox: the
        runner places it (``placement.py``), prepares its working copy and runs its turns, in
        the step's own dispatcher slot (``dispatch``: the step's reservation – work in flight
        goes first). Returns how it went – (outcome, prepared) – for the phase to interpret
        (the meaning – a report's, a failure's – is the workflow's)."""
        work, prepared = self.agents.work(node, role, plan, judged_sha, wanted=wanted,
                                          started=started)
        outcome = await dispatch.run(work)
        async with self.c.lock:
            if prepared.run is not None:
                self.agents.finish(prepared.run, outcome)     # however the work ended
        return outcome, prepared

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
                    answer = await self.turn(card.node, card.role, sender, text, task_id)
                except (NoAgent, WorkflowError) as e:
                    self.c.host.fail_message(self.c.cid, card.id, task_id, str(e))
                else:              # (no effect if it replied with a2a_reply already)
                    self.c.host.reply_message(self.c.cid, card.id, task_id, "completed", answer)
                await asyncio.sleep(0)             # its sender takes it out of the inbox
        finally:
            self._busy.discard(card.id)

    # ------------------------------------------------------------------ turns
    async def turn(self, node_id: str, role: str, sender: str, text: str,
                   task_id: str | None = None) -> str:
        """An agent not running answers ``text`` (the message's ``task_id``) in a turn of its
        own – a consult: started if it has not, else resumed in place (``AgentRuns.consult``) –
        in a dispatcher slot, like every sandbox. Its final message, the answer if it did not
        reply with ``a2a_reply`` itself."""
        ready = lambda: self.sandboxes.environment_built(role)  # noqa: E731
        return await self.dispatcher.run(_TurnWork(
            ready, lambda: self.agents.consult(node_id, role, sender, text, task_id)))
