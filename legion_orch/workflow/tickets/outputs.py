"""Named outputs, and what a sign-off signs (plan: ~/plans/tickets-3b.md).

A role declares the output names it must produce; an agent's accepted report says which documents
they are, and the ticket pins them (``Ticket.outputs``: name -> ``doc:<path>@<version>``, or a list
of them). A sign-off names outputs, never paths – ``signs: [spec, tickets]`` – and they resolve
against the work it checks: its owner's outputs, else the one ticket in the owner's subtree that
produces the name. ``head`` is the commit the owner's criteria judge. A passing sign-off moves the
change's pointers (the root's) to exactly what it signed; consumers name pointers in ``inputs``.

The same resolution checks a workflow file (:meth:`.WorkflowSpec.subjects_problems`), with the
roles' declared outputs standing in for the reports'."""
from __future__ import annotations

from collections.abc import Callable, Mapping

from .model import Status, Ticket

HEAD = "head"                                   # the commit its owner's criteria judge
TIER = "tier"                                   # what H-1 applies: the tier's tickets
CLOSED = {Status.FAILED, Status.CANCELLED}

Has = Callable[[Ticket, str], bool]


def pinned(x: Ticket, name: str) -> bool:
    """``x``'s accepted report named ``name`` (at run time)."""
    return name in x.outputs


def producers(t: Mapping[str, Ticket], owner: str, name: str, has: Has = pinned) -> list[Ticket]:
    """Who produced ``name`` for the work ``owner`` checks: the owner, if it did; else every
    live ticket in its subtree that did."""
    x = t[owner]
    if has(x, name):
        return [x]
    out, todo = [], [m for g in x.subtasks for m in g]
    while todo:
        y = t[todo.pop(0)]
        if y.status in CLOSED:
            continue
        if has(y, name):
            out.append(y)
        todo += [m for g in y.subtasks for m in g]
    return out


def subjects(t: Mapping[str, Ticket], signoff: str, has: Has = pinned
             ) -> tuple[dict[str, Ticket | None], list[str]]:
    """Each subject of a sign-off and the ticket that produced it (None: ``head``) – and what
    cannot be resolved (no producer, or several)."""
    s = t[signoff]
    owner = s.parent if s.role == "criterion" else s.id
    out: dict[str, Ticket | None] = {}
    errs = []
    for name in s.signs:
        if name == HEAD:
            out[name] = None
            continue
        found = producers(t, owner, name, has) if owner else []
        if len(found) == 1:
            out[name] = found[0]
        else:
            errs.append(f"{signoff} signs {name!r}: " + (
                f"{owner} has no ticket that produces it" if not found else
                f"several tickets produce it ({', '.join(x.id for x in found)})"))
    return out, errs


def resolve(t: Mapping[str, Ticket], signoff: str, head: str | None
            ) -> tuple[dict[str, str | list[str]], list[str]]:
    """A sign-off's subjects as what it is asked to approve: each output's pinned documents,
    ``head`` the judged commit (``git:<sha>``)."""
    found, errs = subjects(t, signoff)
    out: dict[str, str | list[str]] = {}
    for name, x in found.items():
        if x is None:
            if head is None:
                errs.append(f"{signoff} signs the head, but there is no commit to judge")
            else:
                out[name] = f"git:{head}"
        else:
            out[name] = x.outputs[name]
    return out, errs
