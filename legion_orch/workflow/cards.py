"""The A2A cards of a change: who its agents are, and their ids and URLs.

A card says only who an agent is – id, step, role, title – fixed for the card's life; what its
agent is doing is the step's state (``step_status``) and its tasks'. So cards are **published**:

* the change's own – the **orchestrator** (the controller itself, not a model: it lists the
  cards and asks a human) and the **resolver** (the change's agent for escalations and stuck
  agents) – when the change starts, until it ends;
* one per **step agent** – a role of a step (``implementer@build:T1``, ``reviewer@build:T1``) –
  when the step starts (``ActiveStep.start``), until it ends (accepted, sent back, ended, or its
  node dropped by a fix round). A message to an agent not started starts it; to a stopped one,
  resumes its session (``engine/services/inbox.py``).

They are pushed to the A2A catalog (``server/a2a.py``) as a side effect of the workflow
(``EngineHost.publish_cards`` / ``remove_cards``) – a view of it, not part of the change's
record; a catalog that subscribes is given every change's cards again (a restart). The engine
itself goes by :func:`cards` (the same view), never by the catalog. Verifiers run no
agent and have no card; nor do the controller's own phases.
"""
from __future__ import annotations

from urllib.parse import quote

from .model import Card, Node
from .view import ChangeView

ORCHESTRATOR = "orchestrator"
RESOLVER = "resolver"            # the change's resolver: its node id and role (not a step)


def card_id(role: str, node: str) -> str:
    """A card's id: ``<role>@<node>`` for a step agent (``reviewer@build:T1``); the change's
    own agents by name (``orchestrator``, ``resolver``)."""
    if role == node and role in (ORCHESTRATOR, RESOLVER):
        return role
    return f"{role}@{node}"


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


def step_cards(node: Node) -> list[Card]:
    """The cards a step publishes when it starts: one per agent role (producer, critic, each
    judge of its panel)."""
    return [Card(id=card_id(role, node.id), node=node.id, role=role,
                 title=f"{role} of {node.id}: {node.title}") for role in node.agent_roles]


def change_cards(change_id: str) -> list[Card]:
    """The change's own cards, published for its whole life."""
    return [Card(id=ORCHESTRATOR, title=f"Orchestrator of change {change_id}"),
            Card(id=RESOLVER, node=RESOLVER, role=RESOLVER,
                 title=f"Resolver of change {change_id}: escalations and stuck agents")]


def cards(e: ChangeView) -> list[Card]:
    """The cards the workflow publishes, as it stands now: the change's own while it is
    active, and each step in progress's (one per agent role recorded when it started). What the
    engine itself goes by; the A2A catalog is built from the same cards as they are pushed."""
    ch = e.change
    if ch is None or ch.status != "ACTIVE":
        return []
    out = change_cards(ch.id)
    for node in e.nodes.values():
        if node.status not in ("PENDING", "ACCEPTED", "FAILED"):
            out += step_cards(node)
    return out


def ordered(e: ChangeView, published: list[Card]) -> list[Card]:
    """``published`` cards in order: the change's own first, then the steps' in node order."""
    order = {nid: i for i, nid in enumerate(e.nodes)}
    own = {ORCHESTRATOR: -2, RESOLVER: -1}
    return sorted(published,
                  key=lambda c: (own.get(c.id, order.get(c.node or "", len(order))), c.id))


def agent_state(e: ChangeView, card: Card) -> str:
    """Not in the card: whether its agent has a sandbox, running or stopped (for views only –
    "orchestrator" for the controller's card)."""
    if card.node is None:
        return "orchestrator"
    runs = [r for r in e.runs.values() if r.node_id == card.node and r.role == card.role
            and r.sandbox and r.sandbox_ref and not r.sandbox_deleted]
    if not runs:
        return "not started"
    r, node = runs[-1], e.nodes.get(card.node)
    stopped = r.sandbox_stopped or (node is not None and node.sandbox == r.sandbox
                                    and node.sandbox_stopped)
    return "stopped" if stopped else "running"
