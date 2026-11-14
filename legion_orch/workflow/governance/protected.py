"""Protected paths (acceptance tests, oracle support files, configured paths) and the human
approvals that let one task change them (G-PP1)."""
from __future__ import annotations

from ...git import path_matches
from .. import risk
from ..model import (
    Oracle,
    ProtectedPathApproval,
)
from ..view import ChangeView
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
    rec.attestation = e.write_record(f"control/approvals/protected-paths/{n}.json", rec).attestation
    e.change.protected_approvals.append(rec)
    return rec


def protected_paths(e: ChangeView) -> list[str]:
    orc = e.change.oracle or Oracle()
    return list(orc.acceptance_tests) + list(orc.protected_support) \
        + list(e.cfg.protected_paths)


def approved_protected_for(e: ChangeView, task_id: str, before: str) -> list[str]:
    """Guard G-PP1: protected paths this task may touch in a run created at ``before``."""
    return [p for a in e.change.protected_approvals if a.task == task_id and a.at < before
            for p in a.paths]


def all_approved_protected(e: ChangeView) -> list[str]:
    return [p for a in e.change.protected_approvals for p in a.paths]
