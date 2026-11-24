"""A replan as a document: the planner's ``tickets`` output (the first replan), a resolver's
proposal (later). It parses into :class:`.Replan`, which the rules apply when the sign-off that
signs it passes.

::

    {"add": [{"into": "delivery", "group": 0, "join": false,
              "ticket": {"id": "T1", "type": "agent:implementer", "goal": "…",
                         "labels": [], "inputs": ["spec"], "work": {…},
                         "criteria": [{"id": "verify:T1", "type": "controller:verify"}, …],
                         "subtasks": [[{…}, …], …]}}],
     "cancel": ["id"], "fail": ["id"], "update": {"id": {"goal": "…"}}}

``into``: the ticket it goes under; ``group``: the index of its subtask group there (``join``:
into that group, else a new group at that index); ``criterion: true``: as a criterion of
``into`` at that index. A ticket's ``criteria`` and ``subtasks`` come with it (nested). A
criterion's ``severity`` defaults to ``rework`` – ``escalate`` on a grouping ticket (R4)."""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .model import GROUPING, Severity, Ticket
from .rules import UPDATABLE, Add, Replan

TICKET_KEYS = {"id", "type", "goal", "labels", "inputs", "work", "criteria", "subtasks",
               "severity", "waivable", "signs", "applies", "limit"}


def parse_plan(data: Any) -> tuple[Replan, list[str]]:
    """The replan a document describes, and what is wrong with it (nothing applies then)."""
    v: list[str] = []
    if not isinstance(data, dict):
        return Replan(), ["the plan is not a JSON object"]
    if bad := sorted(set(data) - {"add", "cancel", "fail", "update"}):
        v.append(f"unknown keys {bad}")
    adds = []
    for i, a in enumerate(data.get("add") or []):
        where = f"add[{i}]"
        if not isinstance(a, dict) or not isinstance(a.get("ticket"), dict):
            v.append(f"{where}: needs a ticket")
            continue
        if not isinstance(a.get("into"), str) or not isinstance(a.get("group", 0), int):
            v.append(f"{where}: needs `into` (a ticket id) and `group` (an index)")
            continue
        tree: list[Ticket] = []
        top = _ticket(a["ticket"], None, "criterion" if a.get("criterion") else "subtask",
                      tree, v, where)
        if top is not None:
            adds.append(Add(top, a["into"], a.get("group", 0), new_group=not a.get("join"),
                            criterion=bool(a.get("criterion")), tree=tree))
    update = data.get("update") or {}
    if not isinstance(update, dict):
        v.append("update: an object of ticket id -> fields")
        update = {}
    for tid, fields in update.items():
        if not isinstance(fields, dict) or (bad := set(fields) - UPDATABLE):
            v.append(f"update {tid}: only {sorted(UPDATABLE)} can be updated")
    cancel, fail = lists(data, "cancel", v), lists(data, "fail", v)
    seen = [x.id for a in adds for x in [a.ticket, *a.tree]]
    if dup := sorted({i for i in seen if seen.count(i) > 1}):
        v.append(f"duplicate ticket ids {dup}")
    return Replan(cancel=cancel, fail=fail, add=adds, update=dict(update)), v


def lists(data: dict, key: str, v: list[str]) -> list[str]:
    value = data.get(key) or []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        v.append(f"{key}: a list of ticket ids")
        return []
    return value


def _ticket(d: dict, parent: str | None, role: str, tree: list[Ticket], v: list[str],
            where: str, parent_type: str | None = None) -> Ticket | None:
    """One ticket of the document and – into ``tree``, parents first – everything under it
    (the top ticket, ``parent`` None, is the Add's own)."""
    if bad := sorted(set(d) - TICKET_KEYS):
        v.append(f"{where}: unknown keys {bad}")
    fields = {k: d[k] for k in TICKET_KEYS - {"criteria", "subtasks"} if k in d}
    if role == "criterion" and "severity" not in fields:
        fields["severity"] = Severity.ESCALATE if parent_type == GROUPING else Severity.REWORK
    try:
        x = Ticket(parent=parent, role=role, **fields)  # type: ignore[arg-type]
    except ValidationError as e:
        v.append(f"{where}: {e.errors()[0]['loc']} {e.errors()[0]['msg']}")
        return None
    if parent is not None:
        tree.append(x)
    for gi, group in enumerate(d.get("subtasks") or []):
        if not isinstance(group, list):
            group = [group]
        ids = []
        for mi, m in enumerate(group):
            sub = f"{where}.subtasks[{gi}][{mi}]"
            if not isinstance(m, dict):
                v.append(f"{sub}: not a ticket")
                continue
            y = _ticket(m, x.id, "subtask", tree, v, sub, x.type)
            if y is not None:
                ids.append(y.id)
        if ids:
            x.subtasks.append(ids)
    for ci, c in enumerate(d.get("criteria") or []):
        sub = f"{where}.criteria[{ci}]"
        if not isinstance(c, dict):
            v.append(f"{sub}: not a ticket")
            continue
        y = _ticket(c, x.id, "criterion", tree, v, sub, x.type)
        if y is not None:
            x.criteria.append(y.id)
    return x


def depends(plan: Replan) -> dict[str, list[str]]:
    """Each added ticket and the added tickets finished before it starts, within the same
    parent (earlier groups) – the task graph's ``depends_on``, for the plan guards."""
    out: dict[str, list[str]] = {}
    by_into: dict[str, list[Add]] = {}
    for a in plan.add:
        if not a.criterion:
            by_into.setdefault(a.into, []).append(a)
    for adds in by_into.values():
        groups: list[list[str]] = []
        for a in sorted(adds, key=lambda a: a.group):
            if a.new_group or not groups:
                groups.append([a.ticket.id])
            else:
                groups[-1].append(a.ticket.id)
        for i, g in enumerate(groups):
            for tid in g:
                out[tid] = [y for earlier in groups[:i] for y in earlier]
    return out
