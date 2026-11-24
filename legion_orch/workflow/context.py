"""What an agent may know about where its work stands (MCP ``ticket_status``,
``workflow_context``), derived from the change's state alone (like :mod:`.attention` and
:mod:`.cards`).

* :func:`ticket_status` – one ticket as it is now: its status and attempts, what it waits for,
  its criteria and their verdicts, its runs, the findings it was told to fix.
* :func:`workflow_context` – how a ticket fits the change: the goal and tier, its place in the
  tree (its parent, the groups around it, what runs before and after it), its inputs (the
  approved versions it consumes) and who judges it (whom to ask: the A2A catalog's cards).

Agents read these to decide, never to override what their assignment says.
"""
from __future__ import annotations

from typing import Any

from .tickets.rules import precedes
from .view import ChangeView


def ticket_status(e: ChangeView, ticket_id: str) -> dict[str, Any]:
    """One ticket as it is now (KeyError: no such ticket)."""
    t = e.tickets.t[ticket_id]
    return {"ticket": t.id, "type": t.type, "goal": t.goal, "status": str(t.status),
            "waiting": str(t.waiting) if t.waiting else None, "attempt": t.attempt,
            "limit": t.limit, "sha": t.sha, "findings": t.findings[:30],
            "outputs": t.outputs,
            "criteria": [{"ticket": c.id, "type": c.type, "status": str(c.status),
                          "findings": c.findings[:10]}
                         for cid in t.criteria if (c := e.tickets.t.get(cid))],
            "runs": [{"run": r.id, "role": r.role, "attempt": r.attempt, "status": r.status}
                     for rid in t.runs if (r := e.runs.get(rid))]}


def workflow_context(e: ChangeView, ticket_id: str) -> dict[str, Any]:
    """How a ticket fits the change (KeyError: no such ticket)."""
    t = e.tickets.t[ticket_id]
    tickets = e.tickets.t
    live = [x for x in tickets.values() if x.status not in ("failed", "cancelled")]
    parent = tickets.get(t.parent) if t.parent else None
    return {
        "change": {"id": e.cid, "goal": e.change.goal, "tier": e.change.tier},
        "ticket": {"id": t.id, "type": t.type, "goal": t.goal, "labels": sorted(e.tickets.labels(t.id))},
        "parent": None if parent is None else {"id": parent.id, "type": parent.type,
                                               "goal": parent.goal, "groups": parent.subtasks,
                                               "criteria": parent.criteria},
        "before": [x.id for x in live if x.id != t.id and precedes(tickets, x.id, t.id)
                   and x.type != "grouping"][:50],
        "after": [x.id for x in live if x.id != t.id and precedes(tickets, t.id, x.id)
                  and x.type != "grouping"][:50],
        "inputs": {i: e.tickets.root.pointers.get(i) for i in t.inputs},
        "judged_by": [c for c in t.criteria],
    }
