"""Node and stage ids (the tree's naming, engine/workflow_tree.py). A fix round's nodes are new
nodes, with unique names suffixed ``.n`` (``plan.2``, ``H-2.2``): safe in a document path (``@``
would end a ``file:<path>@<version>`` attestation; ``-`` is part of gate names). Stages are
``stage:<name>``. Plain functions every layer uses."""
from __future__ import annotations

ROUND = "."            # a fix round's suffix: plan.2


def stage_id(name: str, n: int = 1) -> str:
    return f"stage:{name}" if n == 1 else f"stage:{name}{ROUND}{n}"


def in_round(nid: str, n: int) -> str:
    """A node's id in fix round ``n`` (round 1 is the original work)."""
    return nid if n == 1 else f"{nid}{ROUND}{n}"


def stage_title(sid: str) -> str:
    """``stage:design.2`` -> ``Design (round 2)``; ``stage:round.2`` -> ``Round 2``."""
    name, n = base_id(sid).removeprefix("stage:"), round_of(sid)
    if name == "round":
        return f"Round {n}"
    return name.capitalize() + (f" (round {n})" if n > 1 else "")


def round_of(nid: str) -> int:
    base, _, n = nid.rpartition(ROUND)
    return int(n) if base and n.isdigit() else 1


def base_id(nid: str) -> str:
    """The id without its round suffix."""
    return nid.rpartition(ROUND)[0] if round_of(nid) > 1 else nid
