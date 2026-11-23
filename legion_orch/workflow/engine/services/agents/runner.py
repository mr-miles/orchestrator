"""The agent runner: what the workflow talks to about agents (~/plans/inbox-runner.md).

* The workflow tells it when the change opens and closes; it hears what the change's tickets do
  on the bus (``TicketMoved``) and keeps the cards from that – never reading the workflow.
* Cards travel on the bus (``CardsPublished`` / ``CardsWithdrawn``); the A2A catalog
  subscribes.
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

from .....bus import CardsPublished, CardsWithdrawn, ItemAdded, TicketMoved
from .....util import now_iso
from .... import cards
from ....model import Run, Turn
from ....tickets import Ticket
from ....tickets.executors import Work as TicketWork
from ...base.common import NoAgent, WorkflowError, log
from ...base.core import ChangeCore
from ...base.dispatch import Dispatcher
from ...base.work import Outcome
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
        self._heard: dict[str, cards.Heard] = {}   # the change's tickets, as announced
        self._carded: set[str] = set()             # the tickets whose card is published
        self._open = False                         # the change's own cards are published
        core.svc.bus.subscribe(ItemAdded, lambda e: self.item_added(e.card), cid=core.cid)
        core.svc.bus.subscribe(TicketMoved, self.ticket_moved, cid=core.cid)

    # ------------------------------------------------------------------ the workflow tells it
    def open_change(self) -> None:
        """The change started: its own agents' cards (orchestrator, resolver)."""
        self._open = True
        self._publish(cards.change_cards(self.c.cid))

    def close_change(self) -> None:
        """The change ended: every card goes."""
        self._open, self._carded = False, set()
        self._remove(None)

    def ticket_moved(self, e: TicketMoved) -> None:
        """The bus: a ticket moved – the cards follow what was announced (a criterion's from
        its owner's start, an open ticket's until it is closed). A replay (the change loaded)
        only rebuilds the record: the cards are published again when a catalog subscribes."""
        self._heard[e.ticket] = cards.Heard(e.ticket, e.type, e.role, e.parent, e.status)
        want = {t for t, h in self._heard.items() if cards.carded(self._heard, h)}
        if e.replay:
            self._carded, self._open = want, True
            return
        if not self._open:
            return
        for tid in sorted(want - self._carded):
            self._publish([cards.ticket_card(self._heard[tid])], now=True)
        for tid in sorted(self._carded - want):
            self._remove(tid, now=True)
        self._carded = want

    def cards(self) -> list:
        """The change's cards now: its own, then its tickets' in the order they were heard of."""
        if not self._open:
            return []
        own = cards.change_cards(self.c.cid)
        return cards.ordered(own + [cards.ticket_card(self._heard[t]) for t in self._carded],
                             list(self._heard))

    def republish(self) -> None:
        """A catalog subscribed (a restart): every card open, again – at once (no change is
        being saved)."""
        if cs := self.cards():
            self.c.svc.bus.publish(CardsPublished(self.c.cid, tuple(cs), replay=True))

    def _publish(self, new: list, *, now: bool = False) -> None:
        if not new:
            return
        event = CardsPublished(self.c.cid, tuple(new))
        self.c.svc.bus.publish(event) if now else self.c.emit(event)
        for card in new:
            self.c.event("a2a", f"card registered: {card.id}", card.ticket)

    def _remove(self, ticket_id: str | None, *, now: bool = False) -> None:
        event = CardsWithdrawn(self.c.cid, ticket_id)
        self.c.svc.bus.publish(event) if now else self.c.emit(event)
        self.c.event("a2a", f"cards removed: {ticket_id or 'all of the change'}", ticket_id)

    # ------------------------------------------------------------------ the workflow's work
    async def publish_work(self, ticket: Ticket, role: str, tw: TicketWork, plan: SandboxPlan,
                           *, wanted: Callable[[], bool], started: Callable[[Run], None],
                           dispatch):
        """A ticket's round for its ``role`` agent – its run's task, in the agent's inbox: the
        runner places it (``placement.py``), prepares its working copy and runs its turns, in
        a dispatcher slot (``dispatch`` – work in flight goes first). Returns how it went –
        (outcome, prepared) – for the ticket's executor to read."""
        work, prepared = self.agents.work(ticket, role, tw, plan, wanted=wanted, started=started)
        try:
            outcome = await dispatch.run(work)
        except Exception as e:                  # noqa: BLE001 – the run fails, not the ticket
            log.exception("the work of a %s run failed", role)
            outcome = Outcome(error=f"{type(e).__name__}: {e}")
        async with self.c.lock:
            if prepared.run is not None:
                self.agents.finish(prepared.run, outcome)     # however the work ended
        return outcome, prepared

    # ------------------------------------------------------------------ inbox events
    def item_added(self, card_id: str) -> None:
        """Something waits in ``card_id``'s inbox: answer it if its agent is not running."""
        card = next((c for c in self.c.a2a_cards() if c.id == card_id), None)
        if card is None or card.ticket is None or card_id in self._busy:
            return
        if self.lifecycle.agent_state(card.ticket, card.role) == "live":
            return                             # it reads its inbox itself; turn_ended follows
        self._busy.add(card_id)
        self.c.host.spawn(self._answer_all(card))

    async def _answer_all(self, card) -> None:
        try:
            while True:
                waiting = self.c.host.inbox_messages(self.c.cid, card.id)
                if not waiting:
                    return
                if self.lifecycle.agent_state(card.ticket, card.role) == "live":
                    return                     # a run started: it reads them
                task_id, sender, text = waiting[0]
                try:
                    answer = await self.turn(card.ticket, card.role, sender, text, task_id)
                except (NoAgent, WorkflowError) as e:
                    self.c.host.fail_message(self.c.cid, card.id, task_id, str(e))
                else:              # (no effect if it replied with a2a_reply already)
                    self.c.host.reply_message(self.c.cid, card.id, task_id, "completed", answer)
                await asyncio.sleep(0)             # its sender takes it out of the inbox
        finally:
            self._busy.discard(card.id)

    # ------------------------------------------------------------------ turns
    async def take_turns(self, run: Run, proc) -> int:
        """A run's agent from its first turn to its last (the process ``proc`` started): each
        turn awaited under the run's timers, and – while it ends with the run's task open (no
        accepted report, or no reply ending the task) – another, its session resumed with the
        orchestrator's message. Each is recorded (``run.turns``). Returns the last exit code."""
        c, agents = self.c, self.agents
        n, asked = 1, None
        while True:
            async with c.lock:
                run.turns.append(Turn(n=n, asked=asked[:500] if asked else None))
                c.save()
            rc = await agents._turn(run, proc)
            async with c.lock:
                turn = run.turns[-1]
                turn.ended_at, turn.exit_code = now_iso(), rc
                asked = agents.ask_again_after(run, rc)
                c.save()
            if asked is None:
                return rc
            n += 1
            proc = await agents.continue_turn(run, asked, n)
            async with c.lock:
                c.procs[run.id] = proc
                c.event("agent", f"turn {n}: the agent's session resumed with the "
                                 "orchestrator's message", run.ticket, run.id)
                c.save()

    async def turn(self, ticket_id: str, role: str, sender: str, text: str,
                   task_id: str | None = None) -> str:
        """An agent not running answers ``text`` (the message's ``task_id``) in a turn of its
        own – a consult: started if it has not, else resumed in place (``AgentRuns.consult``) –
        in a dispatcher slot, like every sandbox. Its final message, the answer if it did not
        reply with ``a2a_reply`` itself."""
        ready = lambda: self.sandboxes.environment_built(role)  # noqa: E731
        return await self.dispatcher.run(_TurnWork(
            ready, lambda: self.agents.consult(ticket_id, role, sender, text, task_id)))
