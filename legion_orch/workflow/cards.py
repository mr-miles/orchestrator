"""The A2A cards of a change, derived from its state alone (like :mod:`.attention`).

* The **orchestrator** card – the controller itself, not a model: it lists the change's cards
  and asks a human – registered for as long as the change is active.
* One card per **step agent** – a role of a step (``implementer@build:T1``,
  ``reviewer@build:T1``, ``security@build:T1``) – from the step's start to its end, whether its
  agent has a sandbox yet or not: a message to an agent not started starts it (and its run
  continues that agent later), one to a stopped agent resumes its session for one turn (see
  ``engine/sandboxes/answer.py``).

Verifiers run no agent and have no card. Nothing is registered or removed by hand: the cards
follow the steps (``Node.agent_roles``, set when a step starts) and their runs' sandbox records,
so they survive restarts; the engine records each card's coming and going as an event.
"""
from __future__ import annotations

from urllib.parse import quote

from pydantic import BaseModel

from .model import ACCEPTED, FAILED, PENDING, ChangeStatus
from .view import ChangeView

ORCHESTRATOR = "orchestrator"


def card_id(role: str, node: str) -> str:
    """A step agent's card id: ``<role>@<node>``, e.g. ``reviewer@build:T1``."""
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


class StepCard(BaseModel):
    id: str                       # "orchestrator", or <role>@<node>
    node: str | None = None       # None: the orchestrator
    role: str | None = None
    title: str
    sandbox: str | None = None    # its agent's sandbox (None: not started yet)
    stopped: bool = False         # its sandbox is stopped (a message starts it)

    @property
    def state(self) -> str:
        if self.node is None:
            return "orchestrator"
        return "not started" if self.sandbox is None else "stopped" if self.stopped else "running"


def in_progress(status: str) -> bool:
    """A step between its start and its end (escalated and waiting steps included)."""
    return status not in (PENDING, ACCEPTED, FAILED)


def cards(e: ChangeView) -> list[StepCard]:
    """The change's cards: the orchestrator first, then – for each step in progress, in node
    order – one per agent role of the step, from the step's start to its end, whether the agent
    has a sandbox yet or not (a message to one that has none starts it)."""
    ch = e.change
    if ch is None or ch.status != ChangeStatus.ACTIVE:
        return []
    out = [StepCard(id=ORCHESTRATOR, title=f"Orchestrator of change {ch.id}")]
    latest: dict[tuple[str, str], object] = {}
    for r in e.runs.values():                 # in creation order: the latest sandbox wins
        if r.sandbox and r.sandbox_ref and not r.sandbox_deleted:
            latest[(r.node_id, r.role)] = r
    for nid, node in e.nodes.items():
        if not in_progress(node.status):
            continue
        roles = node.agent_roles or list(dict.fromkeys(role for (n, role) in latest if n == nid))
        for role in roles:
            r = latest.get((nid, role))
            stopped = bool(r) and (r.sandbox_stopped or (node.sandbox == r.sandbox
                                                         and node.sandbox_stopped))
            out.append(StepCard(id=card_id(role, nid), node=nid, role=role,
                                title=f"{role} of {nid}: {node.title}",
                                sandbox=r.sandbox if r else None, stopped=stopped))
    return out
