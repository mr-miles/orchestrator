"""The in-process event bus: how the controller's components tell each other what happened.

Components that need to know of something – the A2A catalog of the cards the workflow opens,
the agent runner of what waits in an agent's inbox, the resolver of an escalation, the
verification service of a push – **subscribe** to an event type; whoever makes it happen
**publishes** it, without knowing who listens. One bus per controller (``Controller.bus``).

Only **notifications** travel on the bus – fire and forget. **Queries** ("what is this agent
doing?", "what waits in this inbox?") and **requests with a reply** ("deliver this message and
await the answer") stay direct calls on narrow interfaces: a bus with correlation ids would
only rebuild what a call returning a future already is.

Rules:

* **After the commit.** An event a change raises while it holds its lock (``ChangeCore.emit``)
  is queued and published when the change has saved: no subscriber sees state that may still be
  rolled back. Events from outside a change's lock (the A2A inbox, the git remote) are published
  at once (:meth:`Bus.publish`).
* **Subscribers never break publishers.** A plain handler runs at once (a catalog is up to date
  when ``publish`` returns), its failure caught and logged; a coroutine handler – one that waits
  on something – runs as a background task (``spawn``), its failure logged there.
* **Order.** One publisher's events reach each subscriber in the order published; there is no
  order across publishers.
* **Scope.** A change's own components subscribe for that change only (``cid=``); the
  controller-wide ones (the A2A catalog) for every change. :meth:`Bus.drop` removes a change's
  subscriptions.

The events – each says which change it is about (``cid``):

==========================  ===========================  =====================================
event                       published by                 subscribed by
==========================  ===========================  =====================================
:class:`CardsPublished`     agent runner (step / change  A2A catalog
                            opens; a replay)
:class:`CardsWithdrawn`     agent runner (step / change  A2A catalog
                            closes)
:class:`ItemAdded`          A2A inbox                    agent runner (a turn if not running)
:class:`TurnEnded`          an agent's run (its turns    A2A inbox (anything left: ItemAdded)
                            are over)
:class:`Escalated`          a change, as it saves        resolver
:class:`Pushed`             the git remote               verification service (verify the tip)
==========================  ===========================  =====================================
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

log = logging.getLogger("legion.bus")


@dataclass(frozen=True)
class Event:
    cid: str                         # the change it is about


@dataclass(frozen=True)
class CardsPublished(Event):
    """Cards the workflow opened (a step started; the change started; a replay)."""
    cards: tuple[Any, ...]           # workflow.model.Card
    replay: bool = False             # published again (a catalog subscribed): no news


@dataclass(frozen=True)
class CardsWithdrawn(Event):
    """A step's cards (``node``), or – ``None`` – all of the change's, are gone."""
    node: str | None


@dataclass(frozen=True)
class ItemAdded(Event):
    """Something waits in ``card``'s inbox (a message, a question back)."""
    card: str


@dataclass(frozen=True)
class TurnEnded(Event):
    """A run of ``role`` on ``node`` is over: what it left in its inbox wants turns of its own."""
    node: str
    role: str


@dataclass(frozen=True)
class Escalated(Event):
    """A step escalated (a failure, not a gate's due decision)."""
    node: str


@dataclass(frozen=True)
class Pushed(Event):
    """A run pushed its branch: its tip is ``sha``."""
    run: str
    sha: str


E = TypeVar("E", bound=Event)
Handler = Callable[[Any], Awaitable[None] | None]


class Bus:
    def __init__(self, spawn: Callable[[Awaitable[None]], None]):
        self._spawn = spawn
        self._subs: dict[type, list[tuple[str | None, Handler]]] = {}

    def subscribe(self, kind: type[E], handler: Callable[[E], Awaitable[None] | None], *,
                  cid: str | None = None) -> None:
        """``handler(event)`` for every ``kind`` event – only those of change ``cid`` when
        given (a change's own components subscribe so; :meth:`drop` removes them). A handler
        returning a coroutine has it run as a background task."""
        self._subs.setdefault(kind, []).append((cid, handler))

    def drop(self, cid: str) -> None:
        """Remove every subscription of change ``cid`` (the change is removed)."""
        for kind, subs in self._subs.items():
            self._subs[kind] = [(o, h) for o, h in subs if o != cid]

    def subscribers(self, kind: type[Event]) -> int:
        return len(self._subs.get(kind, []))

    def publish(self, event: Event) -> None:
        """Tell every subscriber of the event's type (now: the caller is outside a change's
        lock, or the change has saved)."""
        log.debug("%s", event)
        for cid, handler in list(self._subs.get(type(event), [])):
            if cid is not None and cid != event.cid:
                continue
            try:
                out = handler(event)
            except Exception:
                log.exception("%s: a subscriber failed", type(event).__name__)
                continue
            if asyncio.iscoroutine(out):
                self._spawn(out)               # its failure is logged where it runs
