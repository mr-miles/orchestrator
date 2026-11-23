"""Protected paths (acceptance tests, oracle support files, configured paths) and the human
approvals that let one task change them (G-PP1)."""
from __future__ import annotations

from ...git import path_matches
from .. import risk
from ..model import (
    Oracle,
    ProtectedPathApproval,
)
from ..setup import MISE_TOML
from ..view import ChangeView
from .approvals import ENV_SPEC
from .common import GovernanceError


def record_protected_approval(e: ChangeView, approver: str, payload: dict) -> ProtectedPathApproval:
    task, paths, reason = payload.get("task"), payload.get("paths") or [], payload.get("reason")
    builds = {b.task.id: b for b in e.build_nodes()}
    if task not in builds:
        raise GovernanceError(f"unknown task {task!r}")
    protected = protected_paths(e)
    bad = [p for p in paths if not any(path_matches(p, x) for x in protected)]
    if not paths or bad:
        raise GovernanceError(f"paths must be protected acceptance paths {protected}; got {bad or paths}")
    if not risk.valid_reason(reason):
        raise GovernanceError("a reason of ≥24 chars and more than one word is required")
    rec = ProtectedPathApproval(task=task, paths=paths, approver=approver, reason=reason)
    n = len(e.change.protected_approvals) + 1
    e.write_record(f"control/approvals/protected-paths/{n}.json", rec)   # projected
    return e.change.protected_approvals[-1]


def setup_paths(e: ChangeView) -> list[str]:
    """§5.3c: ``mise.toml`` is protected while the change has a pinned setup or its environment
    spec proposes one – a task must not change what runs before every check."""
    if e.change.setup_pin:
        return [MISE_TOML]
    if e.latest(ENV_SPEC):
        try:
            if e.json_doc(ENV_SPEC).get("mise"):
                return [MISE_TOML]
        except (ValueError, AttributeError):
            pass
    return []


def protected_paths(e: ChangeView) -> list[str]:
    orc = e.change.oracle or Oracle()
    return list(orc.acceptance_tests) + list(orc.protected_support) \
        + list(e.cfg.protected_paths) + setup_paths(e)


def approved_protected_for(e: ChangeView, task_id: str, before: str) -> list[str]:
    """Guard G-PP1: protected paths this task may touch in a run created at ``before``."""
    return [p for a in e.change.protected_approvals if a.task == task_id and a.at < before
            for p in a.paths]


def all_approved_protected(e: ChangeView) -> list[str]:
    return [p for a in e.change.protected_approvals for p in a.paths]


async def protected_unchanged(e: ChangeView, head: str) -> list[str]:
    """Guard G-INT2: at ``head``, every protected acceptance test and support file is as the
    oracle committed it, unless a human approved changing it."""
    if not e.change.oracle_sha:
        return []
    approved = all_approved_protected(e)
    orc = e.change.oracle or Oracle()
    v = [f"G-INT2: protected acceptance test {p} changed since oracle commit"
         for p in orc.acceptance_tests + orc.protected_support
         if p not in approved and await e.git.blob(head, p) != await e.git.blob(e.change.oracle_sha, p)]
    pin = e.change.setup_pin
    if pin and pin.path not in approved and await e.git.blob(head, pin.path) != pin.blob:
        v.append(f"G-INT2: {pin.path} changed since its setup was pinned ({pin.source})")
    return v
