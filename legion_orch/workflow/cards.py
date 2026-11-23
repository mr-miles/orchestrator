"""The A2A cards of a change: who its agents are, and their ids and URLs.

A card says only who an agent is – id, ticket, role, title – fixed for the card's life; what its
agent is doing is its ticket's state and its tasks'. So cards are **published**:

* the change's own – the **orchestrator** (the controller itself, not a model: it lists the
  cards and asks a human) and the **resolver** (the change's agent for escalations and stuck
  agents) – when the change starts, until it ends;
* one per **agent ticket** (``implementer@T1``, ``reviewer@review:T1``) while the ticket is open
  – a criterion's from its owner's start – until it is closed. A message to an agent not started
  starts it; to a stopped one, resumes its session.

Which cards exist is decided by whoever keeps them (the agent runner) from what the workflow
announces on the bus (``TicketMoved``) – never by reading the workflow: :func:`carded` is the
rule, over the subscriber's own record (:class:`Heard`).

They are pushed to the A2A catalog (``server/a2a.py``) as a side effect of the workflow
(``EngineHost.publish_cards`` / ``remove_cards``) – a view of it, not part of the change's
record; a catalog that subscribes is given every change's cards again (a restart). The engine
itself goes by :func:`cards` (the same view), never by the catalog. Verifiers run no
agent and have no card; nor do the controller's own tickets.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from .model import Card
from .view import ChangeView

ORCHESTRATOR = "orchestrator"
RESOLVER = "resolver"            # the change's resolver: its card's ticket and role (no ticket's)


def card_id(role: str, ticket: str) -> str:
    """A card's id: ``<role>@<ticket>`` for a ticket's agent (``reviewer@review:T1``); the
    change's own agents by name (``orchestrator``, ``resolver``)."""
    if role == ticket and role in (ORCHESTRATOR, RESOLVER):
        return role
    return f"{role}@{ticket}"


def card_url(a2a_url: str, card: str) -> str:
    """A card's URL on the A2A listener (``server.a2a_url``). No change id: the listener answers
    each caller for its own change, by its identity."""
    return f"{a2a_url}/{quote(card, safe=':@')}"


def catalog_url(a2a_url: str) -> str:
    """The caller's change's AI Catalog (``a2a catalog add``): its cards as catalog entries."""
    return f"{a2a_url}/ai-catalog.json"


def identifier(card: str) -> str:
    """A card's AI Catalog identifier, e.g. ``urn:air:legion:agent:reviewer@build:T1`` (a sandbox
    serves one change, so the card id is unique in its catalog store)."""
    return f"urn:air:legion:agent:{card}"


OPEN = {"working", "subtasks", "checking", "merging", "waiting", "paused", "rejected"}


@dataclass(frozen=True)
class Heard:
    """What a subscriber heard of a ticket (``TicketMoved``): no more than a card needs."""
    ticket: str
    type: str
    role: str
    parent: str | None
    status: str


def carded(heard: dict[str, Heard], t: Heard) -> bool:
    """Whether an agent ticket has a card: while it is open – and, a criterion, from its owner's
    start (an agent not started yet may be asked, and starts to answer)."""
    if not t.type.startswith("agent:"):
        return False
    if t.status in OPEN:
        return True
    owner = heard.get(t.parent) if t.parent else None
    return t.status == "pending" and t.role == "criterion" and owner is not None \
        and owner.status in OPEN


def ticket_card(t: Heard) -> Card:
    role = t.type.partition(":")[2]
    return Card(id=card_id(role, t.ticket), ticket=t.ticket, role=role, title=f"{role} of {t.ticket}")


def change_cards(change_id: str) -> list[Card]:
    """The change's own cards, published for its whole life."""
    return [Card(id=ORCHESTRATOR, title=f"Orchestrator of change {change_id}"),
            Card(id=RESOLVER, ticket=RESOLVER, role=RESOLVER,
                 title=f"Resolver of change {change_id}: escalations and stuck agents")]


def ordered(published: list[Card], order: list[str]) -> list[Card]:
    """``published`` cards in order: the change's own first, then the tickets' in ``order``."""
    pos = {tid: i for i, tid in enumerate(order)}
    own = {ORCHESTRATOR: -2, RESOLVER: -1}
    return sorted(published,
                  key=lambda c: (own.get(c.id, pos.get(c.ticket or "", len(pos))), c.id))


def agent_state(e: ChangeView, card: Card) -> str:
    """Not in the card: whether its agent has a sandbox, running or stopped (for views only –
    "orchestrator" for the controller's card)."""
    if card.ticket is None:
        return "orchestrator"
    runs = [r for r in e.runs.values() if r.ticket == card.ticket and r.role == card.role
            and r.sandbox and r.sandbox_ref and not r.sandbox_deleted]
    if not runs:
        return "not started"
    return "stopped" if runs[-1].sandbox_stopped else "running"
