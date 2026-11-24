"""A build's file scope (G-B3, WORKFLOW.md §5.6): its plan's ``files_modified``, minus its
``files_forbidden`` and the protected paths – grown only by a decision owner's approval.

A build that needs a file outside its scope asks for it (A2A ``request_scope`` to the change's
orchestrator, with a reason); :func:`request` checks the request and says what a decision owner
should know; an approval adds the paths to the build's ``files_modified`` and ``scope_added``
(which lifts ``files_forbidden`` and a guarded path for them). The oracle's tests are never in
reach: only the oracle changes them (§5.4).
"""
from __future__ import annotations

from ..git import path_matches
from .guards.plan import files_clash
from .roles.base import MISE_TOML, guarded, oracle_paths
from .tickets import Status, Ticket
from .view import RoleContext

BUILDS = ("agent:implementer", "agent:scaffolder")
CLOSED = (Status.PASSED, Status.FAILED, Status.CANCELLED)
HOW = ("to change a file outside your scope, ask for it: a2a send to the orchestrator "
       "`/request_scope <paths>: <why your contract needs them>` – a decision owner approves or "
       "denies it")


def _approved(t: Ticket, p: str) -> bool:
    return any(path_matches(p, a) for a in t.scope_added)


def outside(e: RoleContext, t: Ticket, p: str) -> str | None:
    """Why ``p`` is outside the build's scope (None: it is inside): the oracle's, protected,
    forbidden, or not in its files."""
    work = t.work
    if any(path_matches(p, o) for o in oracle_paths(e)):
        return "oracle"
    if _approved(t, p):
        return None
    if any(path_matches(p, g) for g in guarded(e)):
        return "protected"
    if any(path_matches(p, f) for f in work.get("files_forbidden", [])):
        return "forbidden"
    if not any(path_matches(p, a) for a in work.get("files_modified", [])):
        return "outside"
    return None


def guard(e: RoleContext, t: Ticket, changed: list[str]) -> list[str]:
    """G-B3: the commit changes something, and only paths in the build's scope."""
    if not changed:
        return ["G-B3: commit changes no files"]
    v = []
    for p in changed:
        match outside(e, t, p):
            case "oracle":
                v.append(f"G-B3: {p} is one of the oracle's protected tests: only the oracle "
                         "changes them – revert it, and ask oracle@oracle if you think it is wrong")
            case "protected":
                v.append(f"G-B3: {p} is a protected path")
            case "forbidden":
                v.append(f"G-B3: {p} is forbidden for this task")
            case "outside":
                v.append(f"G-B3: {p} is outside the allowed paths")
    if any(not x.startswith(("G-B3: commit", )) and "oracle's protected" not in x for x in v):
        v.append(f"G-B3: revert those changes, or – {HOW}")
    return v


def request(e: RoleContext, t: Ticket, paths: list[str], reason: str) -> tuple[list[str], list[str]]:
    """A build's request for more files: the paths it needs that are not in its scope yet, and
    what a decision owner should know about them. ValueError: the request cannot be made."""
    if t.type not in BUILDS:
        raise ValueError("only a build (implementer, scaffolder) asks for more files")
    if t.status in CLOSED:
        raise ValueError(f"{t.id} is {t.status}: its scope is settled")
    if not reason.strip():
        raise ValueError("say why your contract needs these files")
    clean = list(dict.fromkeys(p.strip() for p in paths if p.strip()))
    if not clean:
        raise ValueError("name the paths you need")
    if bad := [p for p in clean if p.startswith("/") or ".." in p.split("/")]:
        raise ValueError(f"paths are relative to the repository, without '..': {bad}")
    kinds = {p: outside(e, t, p) for p in clean}
    if oracle := [p for p, k in kinds.items() if k == "oracle"]:
        raise ValueError(f"{oracle}: the oracle's protected tests – only the oracle changes them; "
                         "ask oracle@oracle if you think one is wrong")
    new = [p for p, k in kinds.items() if k is not None]
    warnings = []
    for p in new:
        if kinds[p] == "protected":
            warnings.append(f"{p} is a protected path"
                            + (" – the setup always runs the pinned mise.toml, so editing it does "
                               "not change the setup the checks run" if p == MISE_TOML else ""))
        elif kinds[p] == "forbidden":
            warnings.append(f"{p} is forbidden for this build by the plan (files_forbidden: "
                            f"{', '.join(t.work.get('files_forbidden', []))})")
    for other in e.tickets.t.values():
        if other.id != t.id and other.type in BUILDS and other.status not in CLOSED:
            mine = other.work.get("files_modified", [])
            if clash := files_clash(new, mine):
                warnings.append(f"{other.id} ({other.status}) also owns {clash}: approving lets "
                                "both change it – a merge conflict or overlapping work")
    return new, warnings


def approve(t: Ticket, paths: list[str]) -> None:
    """A decision owner's approval: the paths join the build's files (and lift
    ``files_forbidden`` and a guarded path for them)."""
    files = list(t.work.get("files_modified", []))
    t.work = {**t.work, "files_modified": files + [p for p in paths if p not in files]}
    t.scope_added = list(dict.fromkeys([*t.scope_added, *paths]))
