"""Revised outputs and the work that consumed them (plan: ~/plans/tickets.md, "stale
follow-ups"; decided 2026-11-23).

A replan that revises an approved output – a ticket it adds produces, say, a new `environment` –
does not say what follows from that; the orchestrator does, mechanically:

* :func:`resign`: the producing ticket gets a sign-off of what it revises (a copy of the passed
  sign-off that approved those outputs), unless the replan gives it one – nothing it produces
  becomes approved without one – and, if what it revises includes the output the original
  applies (the planner's ``tickets``), it applies the revision: the new builds, the cancelled;
* :func:`followups`: when such a sign-off passes and moves a pointer to a **different** version,
  every finished ticket that consumed the old one (its ``inputs``) and whose type the workflow
  says to redo (``workflow.yaml`` ``stale``: the provisioning, the oracle) gets a new instance
  – the same work, fresh criteria – right after the producer. A redo type has one ticket per
  change, so the type is the work: its instances are told apart by nothing else. Every other finished ticket
  stands: builds' code is committed, and a replan starts from it (new builds, cancelled ones);
  the baseline is a snapshot from before any build.

Pure functions over the tickets: the runner applies what they return as a replan."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from .model import Status, Ticket
from .rules import Add, Replan, _path

SIGNOFF = "human:signoff"
_N = re.compile(r"^(.*)\.([0-9]+)$")


def next_id(tid: str, taken: Iterable[str]) -> str:
    """A new instance's id, ``<id>.<n>`` with the first n not taken (``provision.2`` renewed is
    ``provision.3``): a name only – nothing reads it back."""
    m = _N.match(tid)
    base, taken = (m.group(1) if m else tid), set(taken)
    n = 2
    while f"{base}.{n}" in taken:
        n += 1
    return f"{base}.{n}"


def resign(t: Mapping[str, Ticket], plan: Replan,
           outputs_of: Mapping[str, list[str]]) -> list[str]:
    """Give each ticket ``plan`` adds that produces approved outputs (the root's pointers) a
    sign-off of them, unless it has one: a copy of the latest passed sign-off of those outputs,
    signing just what this ticket produces, applying what the original applies if that is among
    them. Mutates ``plan``; returns the sign-offs added."""
    pointers = set(t[_root(t)].pointers) if t else set()
    passed = [x for x in _in_order(t) if x.type == SIGNOFF and x.status == Status.PASSED]
    taken = set(t) | {y.id for a in plan.add for y in [a.ticket, *a.tree]}
    added: list[str] = []
    for a in plan.add:
        for y in [a.ticket, *a.tree]:
            if y.role == "criterion":
                continue
            revises = [o for o in outputs_of.get(y.type, []) if o in pointers]
            if not revises:
                continue
            own = [c for c in a.tree if c.parent == y.id and c.type == SIGNOFF]
            if any(set(c.signs) & set(revises) for c in own):
                continue
            model = next((s for s in reversed(passed) if set(s.signs) & set(revises)), None)
            if model is None:
                continue
            signs = [s for s in model.signs if s in revises]
            sid = next_id(model.id, taken)
            taken.add(sid)
            y.criteria = [*y.criteria, sid]
            a.tree.append(Ticket(id=sid, parent=y.id, role="criterion", type=SIGNOFF,
                                 goal=model.goal, signs=signs, waivable=model.waivable,
                                 applies=model.applies if model.applies in signs else None))
            added.append(sid)
    return added


def followups(t: Mapping[str, Ticket], signoff: str, signed: Mapping[str, object],
              redo: set[str]) -> Replan:
    """What a passing sign-off's revisions call for: a new instance of every finished ticket of a
    ``redo`` type that consumed an output it moves to a different version – the latest of each
    type (one per change: the type is the work), unless one of that type is still to run – each
    in a new subtask group right after the sign-off's owner, in tree order."""
    root = t[_root(t)]
    changed = {n for n, ref in signed.items() if n in root.pointers and root.pointers[n] != ref}
    s = t[signoff]
    owner = t[s.parent] if s.parent else None
    if not changed or owner is None or owner.parent is None:
        return Replan()
    into = t[owner.parent]
    group = next(i for i, g in enumerate(into.subtasks) if owner.id in g) + 1
    latest: dict[str, Ticket] = {}
    for x in _in_order(t):
        if x.type in redo and x.role == "subtask" and x.status == Status.PASSED \
                and set(x.inputs) & changed:
            latest[x.type] = x
    open_ = {x.type for x in t.values()
             if x.status not in (Status.PASSED, Status.FAILED, Status.CANCELLED)}
    taken = set(t)
    adds: list[Add] = []
    for type_, x in latest.items():
        if type_ in open_:
            continue
        adds.append(renewal(t, x, into.id, group + len(adds), taken))
    return Replan(add=adds)


def renewal(t: Mapping[str, Ticket], x: Ticket, into: str, group: int, taken: set[str],
            findings: Iterable[str] = ()) -> Add:
    """A new instance of ``x`` – the same work, fresh copies of its criteria – as a new subtask
    group at ``group`` under ``into``; ``findings``: what its first round is told to address.
    Mutates ``taken`` (the ids now in use)."""
    nid = next_id(x.id, taken)
    taken.add(nid)
    tree = []
    for cid in x.criteria:
        c = t[cid]
        ncid = next_id(cid, taken)
        taken.add(ncid)
        tree.append(c.model_copy(update={"id": ncid, "parent": nid, "status": Status.PENDING,
                                         "attempt": 0, "findings": [], "waived": False,
                                         "outputs": {}, "runs": [], "sha": None,
                                         "since": "", "waiting": None}))
    fresh = Ticket(id=nid, type=x.type, goal=x.goal, labels=list(x.labels),
                   inputs=list(x.inputs), work=dict(x.work), limit=x.limit, keep=x.keep,
                   findings=list(findings), criteria=[c.id for c in tree])
    return Add(fresh, into, group, new_group=True, tree=tree)


def _root(t: Mapping[str, Ticket]) -> str:
    return next(tid for tid, x in t.items() if x.parent is None)


def _in_order(t: Mapping[str, Ticket]) -> list[Ticket]:
    """The tickets in tree order (earlier groups first, criteria after subtasks)."""
    tt = dict(t)
    return sorted(tt.values(), key=lambda x: [p for _, p in _path(tt, x.id)])
